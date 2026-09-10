import json
import logging
import os

import httpx

from tools.base import BaseTool

from . import config
from .config import OPS_DATA_DIR, QUEUE_DIR, ensure_dirs, mlx_headers, get_json_config
from .history_store import latest_per_vid, load_history

logger = logging.getLogger(__name__)

PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")
PATTERNS_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")

ANALYZE_PROMPT = """你是抖音短视频爆款归因分析师。基于已发布视频的播放表现和脚本内容，提取爆款模式。
输入包含 Top 表现视频（播放高）和尾部差片（播放低）的标题/钩子/结尾/旁白摘要。
请输出严格 JSON（不要任何额外文字、不要 markdown 代码块）：
{
  "winning_topics": ["爆款选题共性，3-6条，如 极端数字反常识/第二人称代入/已知悖论"],
  "winning_hooks": ["爆款3秒钩子模式，2-4条，如 数字+反常/提问+代入"],
  "losing_patterns": ["差片共性，2-4条，如 纯系列编号开头/抽象名词无钩子"],
  "title_formula": "爆款标题公式，一句话，如 数字/极端 + 反常识结论 + 疑问",
  "topic_gen_hint": "给选题生成的回灌提示，告诉它优先什么、避免什么，1-2句"
}
要求：
- 只从数据中归纳，不编造，样本不足时如实说明
- winning/losing 必须可执行（生成端能据此调整）
"""


class PerfAnalyzeTool(BaseTool):
    name = "perf_analyze"
    description = "归因分析：读 stats_history 排行 + published 脚本，用模型提取爆款模式，落盘 winning_patterns.json 供 topic_gen/mlx_script 回灌"
    roles = frozenset({"analyst"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "model": {"type": "string", "description": "模型名，默认 DEFAULT_CHAT_MODEL"},
        "min_samples": {"type": "integer", "description": "归因最少样本数，默认 5，不足则不归因"},
    }

    async def execute(self, model="", min_samples=5, **_):
        model = model or config.DEFAULT_CHAT_MODEL
        ensure_dirs()
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        rows = load_history()
        if not rows:
            logger.warning("perf_analyze: no history")
            return json.dumps({"error": "no history", "patterns_file": ""}, ensure_ascii=False)
        latest = latest_per_vid(rows)
        # 审计0908 F3: estimated 行 plays 被强制置 0 -> 永落 cold 切片 -> 归因提取假"差片共性"
        # -> 回灌生成端 -> 自增强污染闭环 (系统核心价值主张失效). perf_track._rank 已排除,
        # perf_analyze (决策层) 漏修. 止血: 归因输入只取真实数据行.
        all_rows = list(latest.values())
        estimated_count = sum(1 for r in all_rows if r.get("estimated"))
        ranked = sorted(
            [r for r in all_rows if not r.get("estimated")],
            key=lambda r: -r.get("plays", 0),
        )
        if estimated_count:
            logger.warning(
                "perf_analyze excluded %d estimated rows from attribution (total=%d real=%d)",
                estimated_count, len(all_rows), len(ranked),
            )
        if len(ranked) < int(min_samples):
            logger.warning("perf_analyze: samples=%d < min=%d", len(ranked), min_samples)
            return json.dumps(
                {"error": "insufficient samples", "samples": len(ranked), "min": min_samples},
                ensure_ascii=False,
            )
        scripts = self._load_published_scripts()
        n = len(ranked)
        top_n = max(1, n // 5)
        hot = ranked[:top_n]
        # 审计 P3-6: cold 阈值硬编码 50, 与 perf_track COLD_PLAYS_MAX 分叉. 读同源 config.
        try:
            cold_plays_max = int(get_json_config().get("perf_track", {}).get("cold_plays_max", 50))
        except (TypeError, ValueError):
            cold_plays_max = 50
        cold = [r for r in ranked if r.get("plays", 0) < cold_plays_max][-top_n:]
        evidence = self._build_evidence(hot, cold, scripts)
        logger.info(
            "perf_analyze evidence total=%d hot=%d cold=%d scripts=%d",
            n, len(hot), len(cold), len(scripts),
        )
        try:
            patterns = await self._call_model(evidence, model)
        except RuntimeError:
            logger.exception("perf_analyze infra failure (re-raise -> stop_on_tool_error)")
            raise
        if patterns is None:
            logger.warning("perf_analyze model parse failed, keep existing winning_patterns.json")
            return json.dumps({"error": "model analyze parse failed", "patterns_file": PATTERNS_FILE}, ensure_ascii=False)
        out = {
            "updated_at": self._today(rows),
            "samples": n,
            "hot_count": len(hot),
            "winning_topics": patterns.get("winning_topics", []),
            "winning_hooks": patterns.get("winning_hooks", []),
            "losing_patterns": patterns.get("losing_patterns", []),
            "title_formula": patterns.get("title_formula", ""),
            "topic_gen_hint": patterns.get("topic_gen_hint", ""),
        }
        # 原子写: temp + os.replace. 旧实现直接 open(w) truncate, 进程中途 crash (OOM/SIGKILL/OSError)
        # 残留半截 JSON -> topic_gen/mlx_script 下次读 json.load 崩 -> 进化回灌断 (8-20→8-24 静默 4 天根因).
        # 审计 P2-10: 加 file_lock 防 topic_gen 并发读半写状态.
        from .script_helpers import file_lock
        tmp_path = PATTERNS_FILE + ".tmp"
        with file_lock(PATTERNS_FILE):
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, PATTERNS_FILE)
        logger.info("perf_analyze ok written=%s hot=%d hooks=%s", PATTERNS_FILE, len(hot), out["winning_hooks"])
        return json.dumps({"status": "analyzed", "patterns_file": PATTERNS_FILE, **out}, ensure_ascii=False)

    async def _call_model(self, evidence, model):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": evidence},
            ],
            "temperature": 0.4,
            "max_tokens": 2048,
        }
        try:
            async with httpx.AsyncClient(timeout=config.MLX_TIMEOUT, headers=mlx_headers()) as client:
                resp = await client.post(f"{config.MLX_BASE}/v1/chat/completions", json=payload)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            logger.error("perf_analyze mlx unavailable: %s: %s", type(e).__name__, str(e)[:200])
            raise RuntimeError(f"perf_analyze mlx unavailable: {type(e).__name__}")
        except Exception:
            logger.exception("perf_analyze model call infra failed")
            raise RuntimeError("perf_analyze model call infra failed")
        if resp.status_code != 200:
            logger.error("perf_analyze http %s: %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"perf_analyze mlx http {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            logger.exception("perf_analyze response shape invalid: %s", resp.text[:200])
            return None
        parsed = self._extract_json(content)
        if parsed is None:
            logger.error("perf_analyze parse failed: %s", content[:200])
        return parsed

    @staticmethod
    def _build_evidence(hot, cold, scripts):
        lines = ["=== 爆款表现 Top（播放高）==="]
        for r in hot:
            sc = scripts.get(r.get("vid", ""), {})
            lines.append(
                f"- 播放{r.get('plays',0)} 互动率{r.get('interaction_rate',0)} 标题《{r.get('title','')}》"
                f" 钩子类型:{sc.get('hook_type','未知')} 结尾钩子:{(sc.get('ending_hook','') or '')[:30]}"
            )
        lines.append("=== 差片表现（播放<50）===")
        for r in cold:
            sc = scripts.get(r.get("vid", ""), {})
            lines.append(
                f"- 播放{r.get('plays',0)} 标题《{r.get('title','')}》"
                f" 钩子类型:{sc.get('hook_type','未知')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _load_published_scripts():
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
            sc = item.get("script_json", {})
            if isinstance(sc, str):
                try:
                    sc = json.loads(sc)
                except (ValueError, TypeError):
                    sc = {}
            if not isinstance(sc, dict):
                sc = {}
            if not vid:
                title = item.get("title", "")
                vid = f"title:{title}"
            sc.setdefault("hook_variant", item.get("hook_variant", ""))
            out[vid] = sc
        return out

    @staticmethod
    def _today(rows):
        for r in reversed(rows):
            sa = r.get("snapshot_at", "")
            if sa:
                return sa.split(" ")[0]
        return ""

    @staticmethod
    def _extract_json(content):
        # 统一到 script_helpers.extract_json (审计 P2-5).
        from .script_helpers import extract_json
        return extract_json(content)
