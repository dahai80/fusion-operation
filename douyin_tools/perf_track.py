import datetime
import json
import logging
import os

from tools.base import BaseTool

from .config import OPS_DATA_DIR, QUEUE_DIR, ensure_dirs, get_json_config

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(OPS_DATA_DIR, "stats_history.jsonl")
PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")

# 分类阈值: 模块级保留 import 时绑定值 (兼容旧测试 test_declarative_config_override 断言模块名).
# 运行时实际生效靠下方 _perf_cfg() 实时读 (修 A1: 改配置不重启 daemon 也生效).
_PERF_CFG = get_json_config().get("perf_track", {})
HOT_TOP_PERCENT = float(_PERF_CFG.get("hot_top_percent", 0.2))
HOT_PLAYS_MIN = int(_PERF_CFG.get("hot_plays_min", 100))
COLD_PLAYS_MAX = int(_PERF_CFG.get("cold_plays_max", 50))
# 每 vid 最多保留快照数 (滚动 cap, 防 stats_history.jsonl 无界膨胀). rank 只取每 vid 最新一条.
MAX_HISTORY_PER_VID = int(_PERF_CFG.get("max_history_per_vid", 40))
# 触发压缩的总行数阈值 (低于此不动文件, 避免每次 snapshot 都重写整文件).
_PRUNE_THRESHOLD = MAX_HISTORY_PER_VID * 200


def _perf_cfg():
    # 每次调用实时读配置 (修 A1: 模块级绑定在 daemon 长进程不重 import -> 改配置失效).
    return get_json_config().get("perf_track", {})

STAGE_COLD = "cold_start"
STAGE_REC = "recommend"
STAGE_LONG = "long_tail"


class PerfTrackTool(BaseTool):
    name = "perf_track"
    description = "视频表现追踪与排行：snapshot 抓取全量 stats 落盘 stats_history.jsonl（按 vid 累积多时间点快照+生命周期阶段+互动率）；rank 读历史按播放/互动率排序分类爆款/平庸/差片"
    parameters = {
        "action": {"type": "string", "description": "snapshot|rank"},
        "stats_json": {"type": "string", "description": "snapshot 时传入 douyin_stats 返回的 JSON，空则内部调用 douyin_stats"},
    }

    async def execute(self, action, stats_json="", **_):
        ensure_dirs()
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        if action == "snapshot":
            return await self._snapshot(stats_json)
        if action == "rank":
            return self._rank()
        logger.error("perf_track unknown action=%s", action)
        return json.dumps({"error": f"unknown action {action}"})

    async def _snapshot(self, stats_json):
        stats = self._parse_stats(stats_json)
        if not stats:
            from .douyin_stats import DouyinStatsTool
            logger.info("snapshot: no stats_json, calling douyin_stats")
            t = DouyinStatsTool()
            raw = await t.execute()
            stats = self._parse_stats(raw)
        if not stats:
            logger.error("snapshot empty stats")
            return json.dumps({"error": "empty stats"})
        items = stats.get("items", []) or []
        snap_at = stats.get("fetched_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        written = 0
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for it in items:
                vid = it.get("vid", "")
                if not vid:
                    continue
                plays = it.get("plays", 0) or 0
                likes = it.get("likes", 0) or 0
                comments = it.get("comments", 0) or 0
                shares = it.get("shares", 0) or 0
                age_hours = self._age_hours(it.get("create_time", 0), snap_at)
                inter = round((likes + comments + shares) / max(plays, 1), 4)
                row = {
                    "vid": vid,
                    "title": (it.get("title", "") or "")[:60],
                    "plays": plays,
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "interaction_rate": inter,
                    "create_time": it.get("create_time", 0),
                    "snapshot_at": snap_at,
                    "age_hours": age_hours,
                    "stage": self._stage(age_hours),
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
        rank = self._rank_top(items)
        backfilled = self._backfill_published_vid(items)
        pruned = self._prune_history()
        logger.info("snapshot done written=%d snap_at=%s vid_backfill=%d pruned=%d", written, snap_at, backfilled, pruned)
        return json.dumps(
            {"status": "snapshotted", "written": written, "snapshot_at": snap_at, "top": rank, "vid_backfill": backfilled, "pruned": pruned},
            ensure_ascii=False,
        )

    @staticmethod
    def _prune_history():
        # 滚动 cap: 总行数超阈值时, 每 vid 只留最新 N 条快照重写.
        # rank 只取每 vid 最新一条, 老快照无分析价值 -> 压缩防 jsonl 无界膨胀 (修 A3).
        # 阈值实时读配置 (修 A1): prune_threshold 显式配置优先, 否则 cap*200 兜底.
        cfg = _perf_cfg()
        cap = int(cfg.get("max_history_per_vid", MAX_HISTORY_PER_VID))
        prune_threshold = int(cfg.get("prune_threshold", cap * 200))
        if not os.path.exists(HISTORY_FILE):
            return 0
        rows = PerfTrackTool._load_history()
        if len(rows) < prune_threshold:
            return 0
        by_vid = {}
        for r in rows:
            vid = r.get("vid", "")
            if not vid:
                continue
            by_vid.setdefault(vid, []).append(r)
        kept = []
        for vid, lst in by_vid.items():
            # _load_history 保文件顺序 (旧->新), 取尾部最新 N 条.
            kept.extend(lst[-cap:])
        tmp_path = HISTORY_FILE + ".pruned"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp_path, HISTORY_FILE)
        dropped = len(rows) - len(kept)
        logger.info("prune_history rows=%d kept=%d dropped=%d per_vid_cap=%d", len(rows), len(kept), dropped, cap)
        return dropped

    @staticmethod
    def _backfill_published_vid(items):
        if not os.path.isdir(PUBLISHED_DIR):
            return 0
        by_title = {}
        for it in items:
            t = (it.get("title", "") or "").strip()
            if t and it.get("vid"):
                by_title[t] = it["vid"]
        if not by_title:
            return 0
        n = 0
        for fn in sorted(os.listdir(PUBLISHED_DIR)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(PUBLISHED_DIR, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            if item.get("vid"):
                continue
            title = (item.get("topic") or item.get("title") or "").strip()
            vid = by_title.get(title)
            if not vid:
                for t, v in by_title.items():
                    if title and (title in t or t in title):
                        vid = v
                        break
            if not vid:
                desc = (item.get("description") or "").strip()
                if desc:
                    for t, v in by_title.items():
                        if not t:
                            continue
                        n_cmp = min(len(desc), len(t), 30)
                        if n_cmp > 0 and desc[:n_cmp] == t[:n_cmp]:
                            vid = v
                            break
            if not vid:
                continue
            item["vid"] = vid
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(item, f, ensure_ascii=False)
                os.replace(tmp, path)
                n += 1
            except OSError:
                pass
        return n

    def _rank(self):
        rows = self._load_history()
        if not rows:
            logger.warning("rank: no history")
            return json.dumps({"status": "empty", "ranked": []}, ensure_ascii=False)
        latest = self._latest_per_vid(rows)
        ranked = sorted(latest.values(), key=lambda r: -r.get("plays", 0))
        variant_map = self._load_variant_map()
        classified = []
        for i, r in enumerate(ranked):
            tag = self._classify(r, i, len(ranked))
            r2 = dict(r)
            r2["tag"] = tag
            r2["variant"] = variant_map.get(r.get("vid", ""), "")
            classified.append(r2)
        hot = [r for r in classified if r["tag"] == "hot"]
        mid = [r for r in classified if r["tag"] == "mid"]
        cold = [r for r in classified if r["tag"] == "cold"]
        by_variant = self._aggregate_variant(classified)
        logger.info("rank done total=%d hot=%d mid=%d cold=%d variants=%s", len(classified), len(hot), len(mid), len(cold), by_variant)
        return json.dumps(
            {
                "status": "ranked",
                "total": len(classified),
                "hot": hot[:20],
                "mid": mid[:10],
                "cold": cold[:20],
                "ranked": classified,
                "by_variant": by_variant,
            },
            ensure_ascii=False,
        )

    def _rank_top(self, items):
        ranked = sorted(items, key=lambda x: -x.get("plays", 0))[:10]
        return [
            {
                "vid": i.get("vid", ""),
                "title": (i.get("title", "") or "")[:40],
                "plays": i.get("plays", 0),
                "likes": i.get("likes", 0),
                "comments": i.get("comments", 0),
                "shares": i.get("shares", 0),
            }
            for i in ranked
        ]

    @staticmethod
    def _classify(row, idx, total):
        # 阈值实时读配置 (修 A1), 不依赖模块级绑定值.
        cfg = _perf_cfg()
        hot_top_percent = float(cfg.get("hot_top_percent", HOT_TOP_PERCENT))
        hot_plays_min = int(cfg.get("hot_plays_min", HOT_PLAYS_MIN))
        cold_plays_max = int(cfg.get("cold_plays_max", COLD_PLAYS_MAX))
        plays = row.get("plays", 0)
        if total <= 0:
            return "mid"
        if idx < max(1, int(total * hot_top_percent)) and plays >= hot_plays_min:
            return "hot"
        if plays < cold_plays_max and row.get("stage") in (STAGE_REC, STAGE_LONG):
            return "cold"
        return "mid"

    @staticmethod
    def _stage(age_hours):
        if age_hours < 0:
            return STAGE_LONG
        if age_hours < 24:
            return STAGE_COLD
        if age_hours < 72:
            return STAGE_REC
        return STAGE_LONG

    @staticmethod
    def _age_hours(create_time, snap_at):
        ct = PerfTrackTool._parse_create_time(create_time)
        sa = PerfTrackTool._parse_snap_time(snap_at)
        if ct is None or sa is None:
            return -1
        return round((sa - ct).total_seconds() / 3600.0, 1)

    @staticmethod
    def _parse_create_time(raw):
        if isinstance(raw, (int, float)) and raw > 0:
            try:
                return datetime.datetime.fromtimestamp(int(raw))
            except (ValueError, OSError):
                return None
        if isinstance(raw, str):
            s = raw.replace("发布于", "").strip()
            for fmt in ("%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.datetime.strptime(s, fmt)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _parse_snap_time(raw):
        if isinstance(raw, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.datetime.strptime(raw, fmt)
                except ValueError:
                    continue
        return None

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

    @staticmethod
    def _aggregate_variant(classified):
        agg = {}
        for r in classified:
            v = r.get("variant", "")
            if not v:
                continue
            a = agg.setdefault(v, {"variant": v, "count": 0, "plays_sum": 0, "plays_max": 0})
            a["count"] += 1
            a["plays_sum"] += r.get("plays", 0)
            a["plays_max"] = max(a["plays_max"], r.get("plays", 0))
        for a in agg.values():
            a["plays_avg"] = round(a["plays_sum"] / a["count"], 1) if a["count"] else 0
        return list(agg.values())

    @staticmethod
    def _parse_stats(raw):
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None
        return None
