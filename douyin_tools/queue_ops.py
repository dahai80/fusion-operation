import json
import logging
import os
import time
import uuid

from tools.base import BaseTool

from .config import QUEUE_DIR, ensure_dirs, LEASE_TTL
from .script_helpers import parse_script
from . import queue_store

logger = logging.getLogger(__name__)

# 节点标识: 多节点部署时每节点独立 id, dequeue 写 leased_by 标记归属, archive 前校验.
# 单节点默认空串. DOUYIN_NODE_ID 环境变量覆盖.
NODE_ID = os.environ.get("DOUYIN_NODE_ID", "")

# M3-5: queue 生命周期已迁 SQLite (queue_store.QueueStore). 文件目录 (pending/inflight/
# published/failed) 降为 DB 的投影 (projection), 消费者仍读 JSON 文件. fcntl.flock 删除,
# 并发靠 SQLite WAL + busy_timeout + RLock. 审计 A1: 纯文件无事务/索引/备份.

_store = None
_store_db_path = None


def _db_path():
    # db_path 随 QUEUE_DIR (测试 monkeypatch QUEUE_DIR 后 db 隔离到 tmp).
    return os.path.join(QUEUE_DIR, "queue.db")


def _get_store():
    global _store, _store_db_path
    p = _db_path()
    if _store is None or _store_db_path != p or getattr(_store, "_conn", None) is None:
        if _store is not None:
            try:
                _store.close()
            except Exception as e:
                logger.warning("queue_ops close old store: %s", e)
        _store = queue_store.QueueStore(p)
        _store_db_path = p
        dirs = _dirs()
        # 一次性迁移: DB 空 + 文件存在 → import.
        try:
            queue_store.migrate_from_files(_store, dirs)
        except Exception as e:
            logger.warning("queue_ops migrate_from_files: %s", e)
        # 启动 reconcile: DB 权威修文件 (风险行 line 548).
        if _reconcile_on_startup():
            try:
                drift, _ = queue_store.reconcile_to_files(_store, dirs)
                if drift:
                    logger.warning("queue_ops startup reconcile drift=%d", drift)
            except Exception as e:
                logger.warning("queue_ops startup reconcile: %s", e)
    return _store


def _reconcile_on_startup():
    from .config import get_json_config
    try:
        return bool(get_json_config().get("queue", {}).get("reconcile_on_startup", True))
    except Exception:
        return True


def reset_store():
    # 测试辅助: 清模块级 store 缓存, 下次 _get_store 重建 (tmp db_path).
    global _store, _store_db_path
    if _store is not None:
        try:
            _store.close()
        except Exception:
            pass
    _store = None
    _store_db_path = None


def _dirs():
    return {
        "pending": QueueOpsTool.PENDING_DIR,
        "inflight": QueueOpsTool.INFLIGHT_DIR,
        "published": QueueOpsTool.PUBLISHED_DIR,
        "failed": QueueOpsTool.FAILED_DIR,
    }


class QueueOpsTool(BaseTool):
    name = "queue_ops"
    description = "视频库存队列操作：enqueue 入库待发 / dequeue 取一条待发(原子移入 in-flight) / archive 归档已发+失败回收 / count 统计库存。SQLite(WAL) 事务防并发竞态, 文件目录为 DB 投影"
    roles = frozenset({"publisher", "lead"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "action": {"type": "string", "description": "enqueue|dequeue|archive|requeue|count"},
        "video_path": {"type": "string", "description": "enqueue 时视频绝对路径"},
        "script_json": {"type": "string", "description": "enqueue 时脚本 JSON（含 title/description/narration/scenes）"},
        "topic": {"type": "string", "description": "enqueue 时选题，可空"},
        "item_id": {"type": "string", "description": "archive 时指定的队列项 id"},
        "status": {"type": "string", "description": "archive 时目标状态：published|failed，默认 published"},
        "vid": {"type": "string", "description": "archive published 时写入的抖音作品 id（发布时捕获），可空"},
    }

    PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
    INFLIGHT_DIR = os.path.join(QUEUE_DIR, "inflight")
    PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")
    FAILED_DIR = os.path.join(QUEUE_DIR, "failed")

    async def execute(
        self,
        action,
        video_path="",
        script_json="",
        topic="",
        item_id="",
        status="published",
        vid="",
        **_,
    ):
        ensure_dirs()
        for d in (self.PENDING_DIR, self.INFLIGHT_DIR, self.PUBLISHED_DIR, self.FAILED_DIR):
            os.makedirs(d, exist_ok=True)
        store = _get_store()
        if action == "enqueue":
            return self._enqueue(store, video_path, script_json, topic)
        if action == "dequeue":
            return self._dequeue(store)
        if action == "archive":
            return self._archive(store, item_id, status, vid)
        if action == "requeue":
            return self._requeue(store, item_id)
        if action == "count":
            return self._count(store)
        logger.error("queue_ops unknown action=%s", action)
        return json.dumps({"error": f"unknown action {action}"})

    def _enqueue(self, store, video_path, script_json, topic):
        if not video_path or not os.path.exists(video_path):
            logger.error("enqueue invalid video_path=%s", video_path)
            return json.dumps({"error": "video not exist"})
        # E8: script_helpers.parse_script 单一入口 (ast.literal_eval 回退解析 repr).
        script = parse_script(script_json)
        title = (script.get("title", "") if script else "") or topic or "untitled"
        ts = int(time.time())
        safe_title = "".join(c for c in title if c.isalnum() or c in "_-")[:40]
        # 秒级+标题+uuid4 防 1 秒内同标题并发 enqueue 覆盖.
        item_id = f"{ts}_{safe_title}_{uuid.uuid4().hex[:8]}"
        item = {
            "id": item_id,
            "video_path": video_path,
            "script_json": script if script else script_json,
            "topic": topic,
            "title": title,
            "description": script.get("description", "") if script else "",
            "hook_variant": (script.get("hook_variant") if script else "") or "",
            "status": "pending",
            "created_at": ts,
        }
        store.enqueue(item)
        # 投影: 写 pending JSON (消费者 + ops_health _count_dir 读文件, 兼容).
        d = store.get(item_id)
        if d is not None:
            queue_store.write_projection(d, _dirs())
        pending = store.count().get("pending", 0)
        logger.info("enqueue ok id=%s pending=%d", item_id, pending)
        return json.dumps({"status": "enqueued", "item_id": item_id, "pending": pending}, ensure_ascii=False)

    def _dequeue(self, store):
        # 原子取队首: reclaim_expired → SELECT oldest pending → UPDATE inflight + lease.
        # lease 模型: dequeue 后 LEASE_TTL 秒内未 archive 视崩溃/超时, 下轮 reclaim 回收.
        d = store.dequeue(LEASE_TTL, NODE_ID)
        if d is None:
            logger.info("dequeue empty queue")
            return json.dumps({"status": "empty", "has_stock": "false", "item": {}, "video_path": "", "script_json": "", "title": "", "desc_text": "", "item_id": ""}, ensure_ascii=False)
        # 投影: pending 文件清场 + 写 inflight 文件.
        queue_store.write_projection(d, _dirs())
        script = d.get("script_json")
        if isinstance(script, str):
            try:
                script = json.loads(script)
            except (ValueError, TypeError):
                script = {}
        if not isinstance(script, dict):
            script = {}
        desc_text = script.get("description", "") or d.get("description", "")
        logger.info("dequeue ok id=%s title=%s leased_by=%s until=%s -> inflight", d.get("id"), d.get("title", ""), d.get("leased_by"), d.get("leased_until"))
        return json.dumps({
            "status": "dequeued",
            "has_stock": "true",
            "video_path": d.get("video_path", ""),
            "title": d.get("title", ""),
            "desc_text": desc_text,
            "item_id": d.get("id", ""),
            "topic": d.get("topic", ""),
            "leased_by": d.get("leased_by", ""),
        }, ensure_ascii=False)

    def _requeue(self, store, item_id):
        # 发布失败显式重排 (审计 P0-1/P2-18): inflight → pending, strip lease, 加 requeued_at.
        if not item_id:
            logger.error("requeue empty item_id")
            return json.dumps({"error": "empty item_id"})
        ok = store.requeue(item_id)
        if not ok:
            return json.dumps({"error": "item not in inflight"})
        d = store.get(item_id)
        if d is not None:
            queue_store.write_projection(d, _dirs())
        pending = store.count().get("pending", 0)
        logger.warning("requeue ok id=%s back to pending=%d (publish failed, retry next round)", item_id, pending)
        return json.dumps({"status": "requeued", "item_id": item_id, "pending": pending}, ensure_ascii=False)

    def _archive(self, store, item_id, status, vid=""):
        if not item_id:
            logger.error("archive empty item_id")
            return json.dumps({"error": "empty item_id"})
        result = store.archive(item_id, status, vid, NODE_ID)
        if isinstance(result, dict) and result.get("error"):
            return json.dumps(result, ensure_ascii=False)
        # 投影: inflight/pending 文件清场 + 写 published/failed 文件.
        queue_store.write_projection(result, _dirs())
        pending = store.count().get("pending", 0)
        logger.info("archive ok id=%s status=%s vid=%s pending=%d", item_id, status, vid, pending)
        return json.dumps({"status": "archived", "item_id": item_id, "vid": vid, "pending": pending}, ensure_ascii=False)

    def _count(self, store):
        c = store.count()
        pending = c.get("pending", 0)
        inflight = c.get("inflight", 0)
        published = c.get("published", 0)
        failed = c.get("failed", 0)
        logger.info("count pending=%d inflight=%d published=%d failed=%d", pending, inflight, published, failed)
        return json.dumps({"pending": pending, "inflight": inflight, "published": published, "failed": failed, "has_stock": "true" if pending > 0 else "false"}, ensure_ascii=False)
