import json
import logging
import os
import time

from tools.base import BaseTool

from .config import OPS_DATA_DIR, ensure_dirs
from .script_helpers import file_lock

logger = logging.getLogger(__name__)

WINNING_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")
PENDING_FILE = os.path.join(OPS_DATA_DIR, "pending_patterns.json")


class PatternReviewTool(BaseTool):
    name = "pattern_review"
    description = "winning_patterns 评审门禁 (M3-4 §5.6): list 查看 pending 提案 + 当前转正版 / promote 转正(pending→winning) / reject 驳回(删 pending)"
    roles = frozenset({"analyst", "lead"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "action": {"type": "string", "description": "list|promote|reject, 默认 list"},
    }

    async def execute(self, action="list", **_):
        ensure_dirs()
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        if action == "list":
            return self._list()
        if action == "promote":
            return self._promote()
        if action == "reject":
            return self._reject()
        logger.error("pattern_review unknown action=%s", action)
        return json.dumps({"error": f"unknown action {action}"})

    @staticmethod
    def _load_json(path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError) as exc:
            logger.warning("pattern_review read failed path=%s err=%s", path, exc)
            return None

    def _list(self):
        pending = self._load_json(PENDING_FILE)
        winning = self._load_json(WINNING_FILE)
        logger.info("pattern_review list pending=%s winning=%s", bool(pending), bool(winning))
        return json.dumps(
            {
                "status": "listed",
                "has_pending": "true" if pending else "false",
                "has_winning": "true" if winning else "false",
                "pending": pending or {},
                "winning": winning or {},
                "pending_file": PENDING_FILE,
                "winning_file": WINNING_FILE,
            },
            ensure_ascii=False,
        )

    def _promote(self):
        # 原子转正: read pending -> tmp winning -> os.replace (file_lock 防并发读半写).
        pending = self._load_json(PENDING_FILE)
        if pending is None:
            logger.warning("pattern_review promote no pending file=%s", PENDING_FILE)
            return json.dumps({"error": "no pending patterns to promote", "pending_file": PENDING_FILE})
        tmp_path = WINNING_FILE + ".tmp"
        try:
            with file_lock(WINNING_FILE):
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(pending, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, WINNING_FILE)
            os.replace(PENDING_FILE, PENDING_FILE + ".promoted.%d" % int(time.time()))
        except OSError as exc:
            logger.error("pattern_review promote write failed: %s", exc)
            return json.dumps({"error": "promote write failed", "reason": str(exc)[:200]})
        logger.info("pattern_review promote ok winning=%s (pending archived)", WINNING_FILE)
        return json.dumps(
            {
                "status": "promoted",
                "winning_file": WINNING_FILE,
                "promoted_at": int(time.time()),
                "winning_topics": pending.get("winning_topics", []),
                "winning_hooks": pending.get("winning_hooks", []),
            },
            ensure_ascii=False,
        )

    def _reject(self):
        if not os.path.exists(PENDING_FILE):
            logger.warning("pattern_review reject no pending file=%s", PENDING_FILE)
            return json.dumps({"error": "no pending patterns to reject", "pending_file": PENDING_FILE})
        try:
            archive = PENDING_FILE + ".rejected.%d" % int(time.time())
            os.replace(PENDING_FILE, archive)
        except OSError as exc:
            logger.error("pattern_review reject failed: %s", exc)
            return json.dumps({"error": "reject failed", "reason": str(exc)[:200]})
        logger.info("pattern_review reject ok pending archived=%s", archive)
        return json.dumps({"status": "rejected", "archived_to": archive}, ensure_ascii=False)
