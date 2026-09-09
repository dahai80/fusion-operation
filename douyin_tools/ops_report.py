import datetime
import json
import logging
import os

from tools.base import BaseTool

from .config import OPS_DATA_DIR, QUEUE_DIR

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(OPS_DATA_DIR, "stats_history.jsonl")
WINNING_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")
PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")

STAGE_LABEL = {
    "cold_start": "冷启动<24h",
    "recommend": "推荐期<72h",
    "long_tail": "长尾≥72h",
}


class OpsReportTool(BaseTool):
    name = "ops_report"
    description = "运营统计报表：先全貌（账号+汇总+分布+variant对比），再逐视频细节排行（按指标排序优秀在前），每条标注为什么好/差（归因 winning/losing patterns）。落盘 out/ops/report_YYYY-MM-DD.md"
    roles = frozenset({"analyst"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "stats_json": {"type": "string", "description": "douyin_stats 返回的 JSON"},
        "comments_json": {"type": "string", "description": "评论列表 JSON，可空"},
        "date": {"type": "string", "description": "报表日期 YYYY-MM-DD，空=今天"},
        "sort_by": {"type": "string", "description": "细节排行排序指标 plays|likes|interaction_rate，默认 plays"},
    }

    async def execute(
        self,
        stats_json,
        comments_json="",
        date="",
        sort_by="plays",
        **_,
    ):
        stats = self._parse(stats_json)
        if not stats:
            logger.warning("ops_report empty stats")
            return json.dumps({"error": "empty stats"})
        comments = self._parse_comments(comments_json)
        if not date:
            date = datetime.date.today().strftime("%Y-%m-%d")
        items_for_report = stats.get("items", []) or []
        self._append_history(stats, date)
        prev = self._load_prev(date)
        rows = self._load_history()
        latest = self._latest_per_vid(rows)
        rank_rows = self._merge_rank_rows(items_for_report, latest)
        winning = self._load_winning()
        variant_map = self._load_variant_map()
        report_md = self._build(stats, comments, date, prev, rank_rows, winning, variant_map, sort_by)
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        out_path = os.path.join(OPS_DATA_DIR, f"report_{date}.md")
        # 原子写: temp + os.replace. 旧实现直接 open(w) truncate, 写大报表中途 crash 残留半截 .md.
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        os.replace(tmp_path, out_path)
        logger.info(
            "ops_report done path=%s items=%d ranked=%d comments=%d sort_by=%s",
            out_path, len(items_for_report), len(rank_rows), len(comments), sort_by,
        )
        return json.dumps({"report_path": out_path, "date": date, "ranked": len(latest)}, ensure_ascii=False)

    def _build(self, stats, comments, date, prev, latest, winning, variant_map, sort_by):
        acc = stats.get("account", {}) or {}
        items = stats.get("items", []) or []
        total_plays = sum(i.get("plays", 0) for i in items)
        total_likes = sum(i.get("likes", 0) for i in items)
        total_comments = sum(i.get("comments", 0) for i in items)
        total_shares = sum(i.get("shares", 0) for i in items)
        lines = []
        lines.append(f"# 抖音运营统计报表 · {date}\n")
        # ---- 第一部分：全貌 ----
        lines.append("## 一、全貌")
        lines.append("### 账号概览")
        lines.append(f"- 粉丝：{acc.get('followers_raw', 'N/A')}")
        lines.append(f"- 获赞：{acc.get('total_likes_raw', 'N/A')}")
        lines.append(f"- 作品数：{len(items)}\n")
        lines.append("### 汇总指标")
        lines.append(f"- 总播放：{total_plays}")
        lines.append(f"- 总点赞：{total_likes}")
        lines.append(f"- 总评论：{total_comments}")
        lines.append(f"- 总分享：{total_shares}")
        if items:
            avg_plays = round(total_plays / len(items), 1)
            avg_ir = round(sum(i.get("interaction_rate", 0) for i in items) / len(items), 4)
            lines.append(f"- 均播放/作品：{avg_plays}")
            lines.append(f"- 均互动率：{avg_ir}")
        if prev:
            d_play = total_plays - prev.get("total_plays", 0)
            d_like = total_likes - prev.get("total_likes", 0)
            lines.append("\n### 趋势对比（相比上次采集）")
            lines.append(f"- 播放变化：{'+' if d_play >= 0 else ''}{d_play}")
            lines.append(f"- 点赞变化：{'+' if d_like >= 0 else ''}{d_like}")
        # 分布
        dist = {"hot": 0, "mid": 0, "cold": 0}
        for r in latest:
            tag = self._classify_simple(r, len(latest))
            dist[tag] = dist.get(tag, 0) + 1
        lines.append("\n### 表现分布")
        lines.append(f"- 爆款(hot)：{dist.get('hot', 0)}")
        lines.append(f"- 平稳(mid)：{dist.get('mid', 0)}")
        lines.append(f"- 差片(cold)：{dist.get('cold', 0)}")
        # variant 对比
        vsum = {}
        for r in latest:
            vid = r.get("vid", "")
            v = variant_map.get(vid, "")
            if not v:
                continue
            s = vsum.setdefault(v, {"count": 0, "plays_sum": 0, "plays_max": 0})
            s["count"] += 1
            s["plays_sum"] += r.get("plays", 0)
            s["plays_max"] = max(s["plays_max"], r.get("plays", 0))
        if vsum:
            lines.append("\n### 钩子变体 A/B/C 对比")
            lines.append("| 变体 | 样本 | 均播放 | 最高播放 |")
            lines.append("|------|------|--------|----------|")
            for v in sorted(vsum.keys()):
                s = vsum[v]
                avg = round(s["plays_sum"] / s["count"], 1) if s["count"] else 0
                lines.append(f"| {v} | {s['count']} | {avg} | {s['plays_max']} |")
        # ---- 第二部分：逐视频细节排行 ----
        lines.append("\n## 二、逐视频细节排行（按 " + sort_by + " 降序，优秀在前）")
        sort_key = self._sort_key(sort_by)
        ranked = sorted(latest, key=sort_key)
        lines.append("| # | 标题 | 播放 | 赞 | 评 | 分享 | 互动率 | 阶段 | 变体 | 为什么 |")
        lines.append("|---|------|------|----|----|------|--------|------|------|--------|")
        for i, r in enumerate(ranked, 1):
            tag = self._classify_simple(r, len(latest))
            why = self._explain_why(r, tag, winning)
            title = (r.get("title", "") or "")[:32]
            stage = STAGE_LABEL.get(r.get("stage", ""), r.get("stage", "--"))
            ir = r.get("interaction_rate", 0)
            v = variant_map.get(r.get("vid", ""), "-")
            lines.append(
                f"| {i} | {title} | {r.get('plays', 0)} | {r.get('likes', 0)} | "
                f"{r.get('comments', 0)} | {r.get('shares', 0)} | {ir} | {stage} | {v} | {why} |"
            )
        # ---- 第三部分：归因总结 ----
        lines.append("\n## 三、归因总结（为什么好/差）")
        if winning:
            if winning.get("winning_topics"):
                lines.append("### 爆款共性")
                for p in winning["winning_topics"][:3]:
                    lines.append(f"- ✅ {p}")
            if winning.get("losing_patterns"):
                lines.append("\n### 差片共性")
                for p in winning["losing_patterns"][:3]:
                    lines.append(f"- ❌ {p}")
            if winning.get("title_formula"):
                lines.append(f"\n### 标题公式\n`{winning['title_formula']}`")
        else:
            lines.append("- 暂无 winning_patterns，先跑 perf_analyze 归因")
        # ---- 评论 + 建议 ----
        lines.append("\n## 四、新评论（待回复）")
        if comments:
            for c in comments[:10]:
                lines.append(f"- {(c.get('text', '') or '')[:80]}")
        else:
            lines.append("- 无新评论")
        lines.append("\n## 五、建议")
        if total_plays == 0:
            lines.append("- 播放为0，检查作品是否发布成功")
        else:
            lines.append("- 复用爆款共性选题与钩子，淘汰差片 pattern")
        lines.append("- 评论1h内集中回复，提升互动率")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _merge_rank_rows(items, latest_by_vid):
        # 主源 stats.items(当次采集, 有 vid/title/plays/likes/comments/shares),
        # 用 stats_history latest 按 vid 补 stage/interaction_rate(perf_track 算的).
        # 若 items 空则回退用 history latest 全量.
        if not items:
            return list(latest_by_vid.values())
        out = []
        for it in items:
            vid = it.get("vid", "")
            row = dict(it)
            hist = latest_by_vid.get(vid, {})
            if hist:
                if not row.get("stage"):
                    row["stage"] = hist.get("stage", "")
                if not row.get("interaction_rate"):
                    row["interaction_rate"] = hist.get("interaction_rate", 0)
                if not row.get("shares"):
                    row["shares"] = hist.get("shares", row.get("shares", 0))
            if not row.get("stage"):
                row["stage"] = ""
            if not row.get("interaction_rate"):
                row["interaction_rate"] = 0
            out.append(row)
        return out

    @staticmethod
    def _sort_key(sort_by):
        if sort_by == "likes":
            return lambda r: -r.get("likes", 0)
        if sort_by == "interaction_rate":
            return lambda r: -r.get("interaction_rate", 0)
        return lambda r: -r.get("plays", 0)

    @staticmethod
    def _classify_simple(row, total):
        plays = row.get("plays", 0)
        if total <= 0:
            return "mid"
        if plays < 50 and row.get("stage") in ("recommend", "long_tail"):
            return "cold"
        if plays >= _plays_threshold(total):
            return "hot"
        return "mid"

    @staticmethod
    def _explain_why(row, tag, winning):
        title = row.get("title", "") or ""
        plays = row.get("plays", 0)
        ir = row.get("interaction_rate", 0)
        if not winning:
            if tag == "hot":
                return f"播放{plays}领先"
            if tag == "cold":
                return f"播放{plays}偏低"
            return "平稳"
        topics = winning.get("winning_topics", []) or []
        losing = winning.get("losing_patterns", []) or []
        if tag == "hot":
            for p in topics:
                if _pattern_hit(title, p):
                    return "✅" + p[:30]
            return "✅" + (topics[0][:30] if topics else f"播放{plays}领先")
        if tag == "cold":
            for p in losing:
                if _pattern_hit(title, p):
                    return "❌" + p[:30]
            return "❌" + (losing[0][:30] if losing else f"播放{plays}偏低")
        return f"互动率{ir}"

    @staticmethod
    def _load_history():
        out = []
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except FileNotFoundError:
            return out
        return out

    @staticmethod
    def _latest_per_vid(rows):
        latest = {}
        for r in rows:
            vid = r.get("vid", "")
            if not vid:
                continue
            cur = latest.get(vid)
            if cur is None or r.get("snapshot_at", "") > cur.get("snapshot_at", ""):
                latest[vid] = r
        return latest

    @staticmethod
    def _load_winning():
        try:
            with open(WINNING_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def _load_variant_map():
        out = {}
        if not os.path.isdir(PUBLISHED_DIR):
            return out
        for fn in sorted(os.listdir(PUBLISHED_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(PUBLISHED_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            vid = item.get("vid", "")
            v = item.get("hook_variant", "")
            if not v:
                sc = item.get("script_json", {})
                if isinstance(sc, str):
                    try:
                        sc = json.loads(sc)
                    except (ValueError, TypeError):
                        sc = {}
                if isinstance(sc, dict):
                    v = sc.get("hook_variant", "")
            if vid and v:
                out[vid] = v
        return out

    def _append_history(self, stats, date):
        items = stats.get("items", []) or []
        snap = {
            "date": date,
            "fetched_at": stats.get("fetched_at", ""),
            "followers_raw": (stats.get("account", {}) or {}).get("followers_raw"),
            "total_plays": sum(i.get("plays", 0) for i in items),
            "total_likes": sum(i.get("likes", 0) for i in items),
            "total_comments": sum(i.get("comments", 0) for i in items),
            "item_count": len(items),
        }
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    def _load_prev(self, current_date):
        snaps = []
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            snaps.append(json.loads(line))
                        except ValueError:
                            pass
        except FileNotFoundError:
            return None
        prevs = [s for s in snaps if s.get("date") != current_date]
        if not prevs:
            return None
        return prevs[-1]

    @staticmethod
    def _parse(raw):
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _parse_comments(raw):
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict) and "comments" in v:
                    return v["comments"]
            except (ValueError, TypeError):
                pass
        if isinstance(raw, dict) and "comments" in raw:
            return raw["comments"]
        return []


def _plays_threshold(total):
    if total <= 0:
        return 100
    return max(100, int(total * 0.2))


def _pattern_hit(title, pattern):
    # pattern 形如 "极端假设/灾难场景" 或 "纯系列编号开头、标题过长无冲突" -> 任一关键词命中即算匹配.
    # 旧实现取前 4 个汉字拼成连续子串 -> 顺序敏感, 只命中含 "极端假设" 连续字样的标题, 漏 "灾难场景" 类命中 (修 L2).
    if not pattern or not title:
        return False
    import re
    parts = re.split(r"[/、，,]", pattern)
    for p in parts:
        k = p.strip()
        if len(k) >= 2 and k in title:
            return True
    return False
