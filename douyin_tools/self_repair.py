import json
import logging
import os
import time

import httpx

from tools.base import BaseTool

from . import config
from .config import OPS_DATA_DIR, QUEUE_DIR, ensure_dirs, get_json_config, mlx_headers
from .history_store import load_history

logger = logging.getLogger(__name__)

PATTERNS_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")
BLACKLIST_FILE = os.path.join(OPS_DATA_DIR, "topic_blacklist.json")
PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")
PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
# 差片修复重写标题后进 review 目录待人工确认, 不自动进 pending (修 L3: 同 video_path 字节直传 pending -> 抖音去重/限流 -> 二次伤害).
REVIEW_DIR = os.path.join(QUEUE_DIR, "review")

# 差片阈值: 模块级保留 import 时绑定值 (默认兜底). 运行时实时读 (修 A1: 改配置不重启 daemon 也生效).
_REPAIR_CFG = get_json_config().get("self_repair", {})
REPAIR_THRESHOLD_PLAYS = int(_REPAIR_CFG.get("cold_plays_threshold", 100))
REPAIR_THRESHOLD_INTER = float(_REPAIR_CFG.get("cold_interaction_rate_threshold", 0.01))
REPAIR_MIN_AGE_HOURS = int(_REPAIR_CFG.get("cold_age_hours", 72))


def _repair_cfg():
    # 每次调用实时读配置 (修 A1: 模块级绑定在 daemon 长进程不重 import -> 改配置失效).
    return get_json_config().get("self_repair", {})

RETITLE_PROMPT = """你是抖音爆款标题改写师。根据爆款模式给差片重写标题和描述。
输出严格 JSON（不要任何额外文字、不要 markdown 代码块）：
{"title": "新标题，≤20字，应用爆款公式", "description": "新描述，含2-3个#话题，≤100字"}
"""


class SelfRepairTool(BaseTool):
    name = "self_repair"
    description = "差片自我修复：scan 扫 72h 差片候选 / retitle 换标题+描述重入 pending / blacklist 选题加入黑名单 / reclip 暂未实现(标记 skip)"
    roles = frozenset({"analyst"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "action": {"type": "string", "description": "scan|retitle|reclip|blacklist"},
        "vid": {"type": "string", "description": "retitle/reclip/blacklist 指定视频 vid"},
        "topic": {"type": "string", "description": "blacklist 指定要拉黑的选题文本"},
        "model": {"type": "string", "description": "模型名，默认 DEFAULT_CHAT_MODEL"},
    }

    async def execute(self, action, vid="", topic="", model="", **_):
        model = model or config.DEFAULT_CHAT_MODEL
        ensure_dirs()
        for d in (PUBLISHED_DIR, PENDING_DIR):
            os.makedirs(d, exist_ok=True)
        if action == "scan":
            return self._scan()
        if action == "retitle":
            return await self._retitle(vid, model)
        if action == "reclip":
            logger.warning("self_repair reclip not_impl (重剪视频成本高，本期用 retitle 替代) vid=%s", vid)
            return json.dumps({"status": "skip", "reason": "reclip not implemented, use retitle", "vid": vid}, ensure_ascii=False)
        if action == "blacklist":
            return self._blacklist(topic)
        logger.error("self_repair unknown action=%s", action)
        return json.dumps({"error": f"unknown action {action}"})

    def _scan(self):
        rows = load_history()
        if not rows:
            logger.info("self_repair scan: no history")
            return json.dumps({"status": "empty", "candidates": []}, ensure_ascii=False)
        latest = {}
        for r in rows:
            v = r.get("vid", "")
            if not v:
                continue
            cur = latest.get(v)
            if cur is None or r.get("snapshot_at", "") > cur.get("snapshot_at", ""):
                latest[v] = r
        candidates = []
        published_vids = self._published_vids()
        review_vids = self._review_vids()
        # 阈值实时读配置 (修 A1), 不依赖模块级绑定值.
        rcfg = _repair_cfg()
        min_age = int(rcfg.get("cold_age_hours", REPAIR_MIN_AGE_HOURS))
        cold_plays = int(rcfg.get("cold_plays_threshold", REPAIR_THRESHOLD_PLAYS))
        cold_inter = float(rcfg.get("cold_interaction_rate_threshold", REPAIR_THRESHOLD_INTER))
        for r in latest.values():
            age = r.get("age_hours", -1)
            plays = r.get("plays", 0)
            inter = r.get("interaction_rate", 0)
            if age >= min_age and plays < cold_plays and inter < cold_inter:
                vid = r.get("vid", "")
                if vid and vid not in published_vids:
                    logger.debug("scan skip vid=%s no published item (video file gone)", vid)
                    continue
                if vid in review_vids:
                    logger.debug("scan skip vid=%s already retitled in review/ (await confirm)", vid)
                    continue
                candidates.append({
                    "vid": vid,
                    "title": r.get("title", ""),
                    "plays": plays,
                    "interaction_rate": inter,
                    "age_hours": age,
                })
        candidates.sort(key=lambda x: x["plays"])
        first_vid = candidates[0]["vid"] if candidates else ""
        has_candidate = "true" if candidates else "false"
        logger.info("self_repair scan candidates=%d total=%d first_vid=%s", len(candidates), len(latest), first_vid)
        return json.dumps({
            "status": "scanned",
            "candidates": candidates,
            "count": len(candidates),
            "has_candidate": has_candidate,
            "first_vid": first_vid,
        }, ensure_ascii=False)

    async def _retitle(self, vid, model):
        if not vid:
            logger.error("self_repair retitle empty vid")
            return json.dumps({"error": "empty vid"})
        item = self._find_published(vid)
        if not item:
            logger.error("self_repair retitle not found vid=%s", vid)
            return json.dumps({"error": "published item not found", "vid": vid})
        sc = item.get("script_json", {})
        if isinstance(sc, str):
            try:
                sc = json.loads(sc)
            except (ValueError, TypeError):
                sc = {}
        old_title = item.get("title", "") or (sc.get("title", "") if isinstance(sc, dict) else "")
        old_desc = item.get("description", "") or (sc.get("description", "") if isinstance(sc, dict) else "")
        hint = self._load_pattern_hint()
        new = await self._call_retitle(old_title, old_desc, hint, model)
        if not new:
            logger.error("self_repair retitle model failed vid=%s", vid)
            return json.dumps({"error": "retitle model failed", "vid": vid})
        new_title = new.get("title", old_title)
        new_desc = new.get("description", old_desc)
        if isinstance(sc, dict):
            sc["title"] = new_title
            sc["description"] = new_desc
        new_item = {
            "id": f"repair_{int(time.time())}_{vid}",
            "video_path": item.get("video_path", ""),
            "script_json": sc if sc else item.get("script_json", ""),
            "topic": item.get("topic", ""),
            "title": new_title,
            "description": new_desc,
            "status": "needs_review",
            "created_at": int(time.time()),
            "repaired_from_vid": vid,
            "repair_action": "retitle",
        }
        # 写入 review 目录待人工确认, 不直接进 pending 被 cron 自动发布.
        # 同 video_path 重新上传抖音会去重/限流, 修复变二次伤害, 故需人工审 (修 L3).
        os.makedirs(REVIEW_DIR, exist_ok=True)
        out_path = os.path.join(REVIEW_DIR, f"{new_item['id']}.json")
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_item, f, ensure_ascii=False)
        os.replace(tmp, out_path)
        logger.info("self_repair retitle ok vid=%s new_title=%s -> review %s (needs manual confirm)", vid, new_title, out_path)
        return json.dumps({
            "status": "retitled",
            "vid": vid,
            "old_title": old_title,
            "new_title": new_title,
            "new_desc": new_desc,
            "new_item_id": new_item["id"],
            "enqueued": "false",
            "review_path": out_path,
            "note": "needs manual review before re-publish (avoid dedup/限流 on same video bytes)",
        }, ensure_ascii=False)

    def _blacklist(self, topic):
        if not topic:
            logger.error("self_repair blacklist empty topic")
            return json.dumps({"error": "empty topic"})
        data = {"updated_at": time.strftime("%Y-%m-%d"), "blacklist": []}
        if os.path.exists(BLACKLIST_FILE):
            try:
                with open(BLACKLIST_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError):
                pass
        bl = data.get("blacklist", []) if isinstance(data, dict) else []
        if not isinstance(bl, list):
            bl = []
        if topic not in bl:
            bl.append(topic)
        data = {"updated_at": time.strftime("%Y-%m-%d"), "blacklist": bl}
        tmp = BLACKLIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BLACKLIST_FILE)
        logger.info("self_repair blacklist ok topic=%s total=%d", topic, len(bl))
        return json.dumps({"status": "blacklisted", "topic": topic, "blacklist_count": len(bl)}, ensure_ascii=False)

    async def _call_retitle(self, old_title, old_desc, hint, model):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": RETITLE_PROMPT},
                {"role": "user", "content": f"原标题《{old_title}》原描述：{old_desc}\n爆款模式：{hint or '数字+反常+疑问'}"},
            ],
            "temperature": 0.7,
            "max_tokens": 256,
        }
        try:
            async with httpx.AsyncClient(timeout=config.MLX_TIMEOUT, headers=mlx_headers()) as client:
                resp = await client.post(f"{config.MLX_BASE}/v1/chat/completions", json=payload)
            if resp.status_code != 200:
                logger.error("self_repair retitle http %s: %s", resp.status_code, resp.text[:200])
                return None
            content = resp.json()["choices"][0]["message"]["content"]
            return self._extract_json(content)
        except Exception:
            logger.exception("self_repair retitle call failed")
            return None

    @staticmethod
    def _find_published(vid):
        if not os.path.isdir(PUBLISHED_DIR):
            return None
        for fn in sorted(os.listdir(PUBLISHED_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(PUBLISHED_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            if item.get("vid", "") == vid:
                return item
        return None

    @staticmethod
    def _published_vids():
        out = set()
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
            v = item.get("vid", "")
            if v:
                out.add(v)
        return out

    @staticmethod
    def _review_vids():
        # 差片已 retitle 落 review/ 待人工确认的 vid 集合, scan 据此去重避免重复 retitle 堆积.
        out = set()
        if not os.path.isdir(REVIEW_DIR):
            return out
        for fn in sorted(os.listdir(REVIEW_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(REVIEW_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            v = item.get("repaired_from_vid", "") or item.get("vid", "")
            if v:
                out.add(v)
        return out

    @staticmethod
    def _load_pattern_hint():
        try:
            with open(PATTERNS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError, FileNotFoundError):
            return ""
        if not isinstance(data, dict):
            return ""
        parts = []
        if data.get("title_formula"):
            parts.append(data["title_formula"])
        if data.get("winning_topics"):
            parts.append("选题共性：" + "、".join(str(x) for x in data["winning_topics"][:4]))
        if data.get("losing_patterns"):
            parts.append("避免：" + "、".join(str(x) for x in data["losing_patterns"][:3]))
        return "；".join(parts)

    @staticmethod
    def _extract_json(content):
        # 模型输出残缺 (尾部 ] 误替 } / markdown 围栏) 时简单 find/rfind 截取会 parse failed.
        # 加括号配平扫描定位真实闭合, depth>0 时尝试尾部修复 (补 } / 末尾 ]->}).
        s = content.strip()
        if s.startswith("```"):
            s = s.split("```")[1]
            if s.startswith("json"):
                s = s[4:]
            s = s.strip()
        start = s.find("{")
        if start == -1:
            return None
        brace = 0
        bracket = 0
        end = -1
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
                if brace == 0 and bracket == 0:
                    end = i
                    break
            elif c == "[":
                bracket += 1
            elif c == "]":
                bracket -= 1
        if end == -1:
            # 模型输出截断, 括号未闭合. 尝试多种尾部修复组合, 哪个 parse 成功用哪个:
            # 末尾 ] 可能是提前的数组闭合 (应换 }), 也可能合法. 列候选: 去尾 ] / 补 } / 补 ]+}.
            tail = s[start:].rstrip()
            candidates = []
            if tail.endswith("]"):
                candidates.append(tail[:-1].rstrip() + "}")  # ] 换 } 关对象
                candidates.append(tail[:-1].rstrip() + "}]}")  # ] 换 } 关对象 + 关数组 + 关外层
            if brace > 0:
                candidates.append(tail + "}" * brace)
            if bracket > 0 and brace > 0:
                candidates.append(tail + "]" * bracket + "}" * brace)
            for cand in candidates:
                try:
                    return json.loads(cand)
                except ValueError:
                    continue
        try:
            return json.loads(s[start : end + 1])
        except ValueError:
            return None
