import json
import logging
import os

import httpx

from . import config
from .config import OPS_DATA_DIR, QUEUE_DIR, ensure_dirs, mlx_headers
from tools.base import BaseTool

logger = logging.getLogger(__name__)

PATTERNS_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")
BLACKLIST_FILE = os.path.join(OPS_DATA_DIR, "topic_blacklist.json")
PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")
PENDING_DIR = os.path.join(QUEUE_DIR, "pending")

SYSTEM_PROMPT = """你是抖音科普吸粉选题策划。按以下规则输出严格 JSON（不要任何额外文字、不要 markdown 代码块）：
{
  "topics": [
    {"topic": "选题标题，悬念/反常/提问式，≤16字", "series": "所属系列", "hook_type": "3秒钩子类型：悬念/反常/提问/数字"}
  ]
}
要求：
- 选题要抓人、有信息量、适合短视频科普
- 选题之间不重复、角度多样
- 不出现水印、引流、敏感词
"""


class TopicGenTool(BaseTool):
    name = "topic_gen"
    description = "用 fusion-mlx 批量生成抖音选题（topic/series/hook_type），读取 winning_patterns.json 回灌爆款模式 + 自动排除已发布/黑名单选题"
    parameters = {
        "series": {"type": "string", "description": "所属系列，如 宇宙奥秘"},
        "count": {"type": "integer", "description": "生成选题数量，默认 7"},
        "exclude": {"type": "string", "description": "要排除的已用选题（逗号分隔），可空"},
        "pattern_file": {"type": "string", "description": "winning_patterns.json 路径，空则读默认，不存在则跳过回灌"},
        "blacklist_file": {"type": "string", "description": "topic_blacklist.json 路径，空则读默认"},
        "model": {"type": "string", "description": "模型名，默认 DEFAULT_CHAT_MODEL"},
    }

    async def execute(
        self,
        series="宇宙奥秘",
        count=7,
        exclude="",
        pattern_file="",
        blacklist_file="",
        model="",
        **_,
    ):
        model = model or config.DEFAULT_CHAT_MODEL
        ensure_dirs()
        series = (series or "").strip() or "宇宙奥秘"
        n = max(1, min(int(count), 30))
        excl_list = [s.strip() for s in exclude.split(",") if s.strip()] if exclude else []
        pub_titles = self._load_published_titles()
        pend_titles = self._load_pending_titles()
        excl_list += pub_titles
        excl_list += pend_titles
        excl_list += self._load_blacklist(blacklist_file or BLACKLIST_FILE)
        hint = self._load_pattern_hint(pattern_file or PATTERNS_FILE)
        user_msg = (
            f"系列：{series}\n请生成 {n} 个不重复的吸粉选题。"
            + (f"\n爆款回灌提示：{hint}" if hint else "")
            + (f"\n不要与以下已用/黑名单选题重复：{', '.join(excl_list[:30])}" if excl_list else "")
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.95,
            "max_tokens": 1024,
        }
        try:
            async with httpx.AsyncClient(timeout=config.MLX_TIMEOUT, headers=mlx_headers()) as client:
                resp = await client.post(f"{config.MLX_BASE}/v1/chat/completions", json=payload)
            if resp.status_code != 200:
                logger.error("topic_gen http %s: %s", resp.status_code, resp.text[:200])
                return json.dumps({"error": f"http {resp.status_code}"})
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = self._extract_json(content)
            if parsed is None or not parsed.get("topics"):
                if parsed and parsed.get("topic"):
                    parsed = {"topics": [parsed]}
                    logger.info("topic_gen fallback single->array topic=%s", parsed["topics"][0].get("topic"))
                else:
                    logger.error("topic_gen parse failed: %s", content[:200])
                    return json.dumps({"error": "json parse failed", "raw": content})
            topics = parsed["topics"][:n]
            # 补货分支调用本工具时需把首个选题扁平化为 topic/series/hook_variant,
            # 注入下游 script/img/tts/compose/enqueue 节点 (cron 路径不传 topic).
            # AgentRuntime 扁平插值不支持 JSON 路径, 故顶层输出这些字段.
            first = topics[0] if topics else {}
            flat = {
                "topic": first.get("topic", ""),
                "series": first.get("series", series),
                "hook_type": first.get("hook_type", ""),
            }
            variants = ["A", "B", "C"]
            flat["hook_variant"] = variants[len(pub_titles) % len(variants)]
            logger.info(
                "topic_gen ok count=%d series=%s first_topic=%s variant=%s",
                len(topics), series, flat["topic"], flat["hook_variant"],
            )
            return json.dumps({"topics": topics, "count": len(topics), **flat}, ensure_ascii=False)
        except Exception as e:
            logger.exception("topic_gen failed")
            return json.dumps({"error": str(e)})

    @staticmethod
    def _extract_json(content):
        s = content.strip()
        if s.startswith("```"):
            s = s.split("```")[1]
            if s.startswith("json"):
                s = s[4:]
            s = s.strip()
        start = s.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            if depth > 0:
                tail = s[start:].rstrip()
                cands = [
                    tail + ("}" * depth),
                    tail[:-1] + "}",
                    tail + "}",
                ]
                seen = set()
                for cand in cands:
                    if cand in seen:
                        continue
                    seen.add(cand)
                    try:
                        return json.loads(cand)
                    except ValueError:
                        continue
            return None
        try:
            return json.loads(s[start : end + 1])
        except ValueError:
            return None

    @staticmethod
    def _load_published_titles():
        if not os.path.isdir(PUBLISHED_DIR):
            return []
        out = []
        for fn in sorted(os.listdir(PUBLISHED_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(PUBLISHED_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            t = (item.get("topic") or item.get("title") or "").strip()
            if t:
                out.append(t)
        logger.info("topic_gen exclude published titles=%d", len(out))
        return out

    @staticmethod
    def _load_pending_titles():
        if not os.path.isdir(PENDING_DIR):
            return []
        out = []
        for fn in sorted(os.listdir(PENDING_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(PENDING_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            t = (item.get("topic") or item.get("title") or "").strip()
            if t:
                out.append(t)
        logger.info("topic_gen exclude pending titles=%d", len(out))
        return out

    @staticmethod
    def _load_blacklist(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError, FileNotFoundError):
            return []
        bl = data.get("blacklist", []) if isinstance(data, dict) else []
        logger.info("topic_gen blacklist=%d file=%s", len(bl), path)
        return [str(x).strip() for x in bl if str(x).strip()]

    @staticmethod
    def _load_pattern_hint(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError, FileNotFoundError):
            return ""
        if not isinstance(data, dict):
            return ""
        parts = []
        hint = (data.get("topic_gen_hint") or "").strip()
        if hint:
            parts.append(hint)
        topics = data.get("winning_topics", [])
        if topics:
            parts.append("爆款选题共性：" + "、".join(str(x) for x in topics[:5]))
        hooks = data.get("winning_hooks", [])
        if hooks:
            parts.append("爆款钩子模式：" + "、".join(str(x) for x in hooks[:4]))
        losing = data.get("losing_patterns", [])
        if losing:
            parts.append("避免：" + "、".join(str(x) for x in losing[:4]))
        return "；".join(parts)
