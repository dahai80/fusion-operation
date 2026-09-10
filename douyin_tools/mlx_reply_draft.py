import json
import logging

from . import config
from .config import mlx_headers
from .resilient_http import post_json
from tools.base import BaseTool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是抖音科普账号的评论回复助手。对每条评论生成一条吸粉回复。
硬约束（规则优先，命中则直接用规则回复，不要自由发挥）：
- 含「震撼/牛逼/厉害/哇/强」→ 「谢谢！觉得震撼的话点个赞转发给朋友吧🚀」
- 含「不懂/什么/为啥/为什么/解释」→ 「这个知识点比较硬核，多看几遍就懂了！有啥疑问评论区问我👇」
- 含「A/选A」→ 「选A的朋友很有眼光！👍」
- 含「B/选B」→ 「选B！我也觉得这个更酷😎」
- 含「C/选C」→ 「选C！想象力满分💯」
- 含「关注/已关注」→ 「感谢关注！每天带你看一个AI画出来的知识🌟」
- 含「下期/下一期/下一条」→ 「下期内容已经在做了，敬请期待！有什么想看的可以告诉我📝」
未命中规则时，生成个性化、口语化、带emoji的回复，≤40字，引导互动，不踩敏感词。
输出严格 JSON，不要任何额外文字：
{"drafts":[{"id":"评论id","text":"回复内容","safe":true或false,"reason":"安全判断原因"}]}
safe=false 的情形：含政治/色情/暴力/广告引流/敏感词，或回复超50字。"""


class MlxReplyDraftTool(BaseTool):
    name = "mlx_reply_draft"
    description = "用 fusion-mlx 批量生成评论回复草稿（规则优先 + LLM 兜底），含安全标记"
    roles = frozenset({"analyst"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "comments_json": {"type": "string", "description": "评论列表 JSON 字符串 [{id,text,user}]"},
        "account_role": {"type": "string", "description": "账号人设，如 科普博主"},
        "rules": {"type": "string", "description": "额外规则，可空"},
        "model": {"type": "string", "description": "模型名，默认 DEFAULT_CHAT_MODEL"},
    }

    async def execute(
        self,
        comments_json,
        account_role="科普博主",
        rules="",
        model="",
        **_,
    ):
        model = model or config.DEFAULT_CHAT_MODEL
        comments = self._parse_comments(comments_json)
        if not comments:
            logger.warning("mlx_reply_draft empty comments")
            return json.dumps({"error": "empty comments"})
        comments_brief = json.dumps(
            [{"id": c.get("id", ""), "text": (c.get("text", "") or "")[:120]} for c in comments],
            ensure_ascii=False,
        )
        user_msg = (
            f"账号人设：{account_role}\n"
            f"额外规则：{rules or '无'}\n"
            f"评论列表：{comments_brief}\n"
            f"请对每条评论生成回复。"
        )
        # max_tokens 按评论数动态扩容: 每条 ~200 token + 256 余量.
        # 5 评论 ~1256, 10 评论 ~2256, 避免 1024 硬上限截断多评论输出 (修 L4).
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.7,
            "max_tokens": max(1024, len(comments) * 200 + 256),
        }
        try:
            resp = await post_json(f"{config.MLX_BASE}/v1/chat/completions", payload, service="mlx")
            if resp.status_code != 200:
                logger.error("mlx_reply_draft http %s: %s", resp.status_code, resp.text[:200])
                return json.dumps({"error": f"http {resp.status_code}"})
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = self._extract_json(content)
            if not parsed or "drafts" not in parsed:
                logger.error("mlx_reply_draft parse failed: %s", content[:200])
                return json.dumps({"error": "json parse failed", "raw": content})
            drafts = parsed["drafts"]
            drafts = self._align_ids(drafts, comments)
            drafts = self._local_safety(drafts, comments)
            logger.info("mlx_reply_draft ok drafts=%d safe=%d", len(drafts), sum(1 for d in drafts if d.get("safe")))
            return json.dumps({"drafts": drafts}, ensure_ascii=False)
        except Exception as e:
            logger.exception("mlx_reply_draft failed")
            return json.dumps({"error": str(e)})

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

    @staticmethod
    def _extract_json(content):
        # 括号配平扫描 + 截断修复, 替代脆弱的 rfind("}") (修 M4).
        # rfind 在模型输出残缺 (尾部 ] 误替 } 或嵌套 } 提前闭合) 时解析失败.
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
                cands = [tail + ("}" * depth), tail + "}"]
                for cand in dict.fromkeys(cands):
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
    def _align_ids(drafts, comments):
        aligned = []
        for i, d in enumerate(drafts):
            out = dict(d)
            src = comments[i] if i < len(comments) else {}
            out["id"] = src.get("id", d.get("id", ""))
            out["comment_text"] = (src.get("text", "") or "")[:120]
            out["user"] = src.get("user", "")
            aligned.append(out)
        return aligned

    @staticmethod
    def _local_safety(drafts, comments):
        block = ["广告", "微信", "加我", "http", "政治", "色情", "暴力", "赌博"]
        for d in drafts:
            txt = (d.get("text") or "")
            if len(txt) > 50:
                d["safe"] = False
                d["reason"] = "over 50 chars"
            elif any(w in txt for w in block):
                d["safe"] = False
                d["reason"] = "blocked word"
            elif "safe" not in d:
                d["safe"] = True
                d["reason"] = d.get("reason", "ok")
        return drafts
