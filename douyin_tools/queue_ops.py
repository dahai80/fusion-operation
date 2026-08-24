import fcntl
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager

from tools.base import BaseTool

from .config import QUEUE_DIR, ensure_dirs
from .script_helpers import parse_script

logger = logging.getLogger(__name__)

# 节点标识: 多节点部署时每节点独立 id, dequeue 写 leased_by 标记归属, archive 前校验.
# 单节点默认空串 (本地锁已足够防并发, lease 仅作崩溃恢复用). DOUYIN_NODE_ID 环境变量覆盖.
NODE_ID = os.environ.get("DOUYIN_NODE_ID", "")
# lease 时长 (秒): dequeue 后 5 分钟内未 archive 视为崩溃/超时, 下一轮 dequeue 可抢占回收.
LEASE_TTL = int(os.environ.get("DOUYIN_LEASE_TTL", "300"))


class QueueOpsTool(BaseTool):
    name = "queue_ops"
    description = "视频库存队列操作：enqueue 入库待发 / dequeue 取一条待发(原子移入 in-flight) / archive 归档已发+失败回收 / count 统计库存。文件锁防并发竞态"
    parameters = {
        "action": {"type": "string", "description": "enqueue|dequeue|archive|count"},
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

    @property
    def _lock_file(self):
        # 动态按 QUEUE_DIR 算锁路径, monkeypatch QUEUE_DIR 后测试也走临时队列.
        return os.path.join(QUEUE_DIR, ".queue.lock")

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
        if action == "enqueue":
            return self._enqueue(video_path, script_json, topic)
        if action == "dequeue":
            return self._dequeue()
        if action == "archive":
            return self._archive(item_id, status, vid)
        if action == "count":
            return self._count()
        logger.error("queue_ops unknown action=%s", action)
        return json.dumps({"error": f"unknown action {action}"})

    @contextmanager
    def _lock(self):
        # 排他文件锁, 防多进程/cron 并发 dequeue 重复取同一 item.
        lock_path = self._lock_file
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock = None
        try:
            lock = open(lock_path, "w")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if lock is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

    def _enqueue(self, video_path, script_json, topic):
        if not video_path or not os.path.exists(video_path):
            logger.error("enqueue invalid video_path=%s", video_path)
            return json.dumps({"error": "video not exist"})
        # E8: script_helpers.parse_script 单一入口. 旧 _parse_script 只 json.loads 无
        # ast.literal_eval repr 回退, AgentRuntime 扁平插值后的 dict repr 解析失败 -> None.
        script = parse_script(script_json)
        title = (script.get("title", "") if script else "") or topic or "untitled"
        ts = int(time.time())
        safe_title = "".join(c for c in title if c.isalnum() or c in "_-")[:40]
        # 秒级+标题+uuid4 防 1 秒内同标题并发 enqueue 覆盖 (修 L7).
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
        with self._lock():
            path = os.path.join(self.PENDING_DIR, f"{item_id}.json")
            # 原子写: crash 在 dump 中途不留半截 JSON (下轮 _list_dir json.load 崩 -> 队列卡死).
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(item, f, ensure_ascii=False)
            os.replace(tmp, path)
            pending = len(self._list_pending())
        logger.info("enqueue ok id=%s pending=%d", item_id, pending)
        return json.dumps({"status": "enqueued", "item_id": item_id, "pending": pending}, ensure_ascii=False)

    def _dequeue(self):
        # 原子取队首: 从 pending 移入 in-flight, 写 leased_by/leased_until 标记归属, 返回扁平字段供 publish.
        # lease 模型: dequeue 后 LEASE_TTL 秒内未 archive 视为崩溃/超时, 下一轮 dequeue 可从 inflight 回收该 item (审计 H1 多节点防重复消费).
        # 单节点本地锁已防并发取, lease 仅作崩溃恢复: 上次 dequeue 进程崩了 item 卡 inflight, 不回收就永久丢失库存.
        with self._lock():
            # 先回收过期 lease 的 in-flight item (上轮 dequeue 崩溃遗留), 移回 pending 重新排队.
            reclaimed = self._reclaim_expired()
            if reclaimed:
                logger.info("dequeue reclaimed %d expired-lease items back to pending", reclaimed)
            items = self._list_pending()
            if not items:
                logger.info("dequeue empty queue")
                return json.dumps({"status": "empty", "has_stock": "false", "item": {}, "video_path": "", "script_json": "", "title": "", "desc_text": "", "item_id": ""}, ensure_ascii=False)
            first = items[0]
            src = os.path.join(self.PENDING_DIR, f"{first['id']}.json")
            dst = os.path.join(self.INFLIGHT_DIR, f"{first['id']}.json")
            try:
                os.replace(src, dst)
            except OSError as e:
                logger.error("dequeue move failed id=%s: %s", first["id"], e)
                return json.dumps({"error": f"dequeue move failed: {e}"}, ensure_ascii=False)
            # 写 lease 归属: 节点 id + 过期时间. archive 前校验 leased_by==NODE_ID 防别节点误归档.
            first["leased_by"] = NODE_ID
            first["leased_until"] = int(time.time()) + LEASE_TTL
            tmp = dst + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(first, f, ensure_ascii=False)
            os.replace(tmp, dst)
        script = first.get("script_json", {})
        if isinstance(script, str):
            try:
                script = json.loads(script)
            except (ValueError, TypeError):
                script = {}
        desc_text = ""
        if isinstance(script, dict):
            desc_text = script.get("description", "") or first.get("description", "")
        if not desc_text:
            desc_text = first.get("description", "")
        # 只回扁平字段, 不把整个 item/script_json 塞进插值域 (修 L8).
        logger.info("dequeue ok id=%s title=%s leased_by=%s until=%d -> inflight", first["id"], first.get("title", ""), NODE_ID, first["leased_until"])
        return json.dumps({
            "status": "dequeued",
            "has_stock": "true",
            "video_path": first.get("video_path", ""),
            "title": first.get("title", ""),
            "desc_text": desc_text,
            "item_id": first.get("id", ""),
            "topic": first.get("topic", ""),
            "leased_by": NODE_ID,
        }, ensure_ascii=False)

    def _reclaim_expired(self):
        # 在 _lock 内调用: 扫 inflight, leased_until < now 的 item 移回 pending (上轮 dequeue 崩溃遗留).
        # 返回回收数. 不删 leased_by/leased_until 字段, 留痕可审计.
        if not os.path.isdir(self.INFLIGHT_DIR):
            return 0
        now = int(time.time())
        reclaimed = 0
        for fn in sorted(os.listdir(self.INFLIGHT_DIR)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.INFLIGHT_DIR, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                logger.warning("reclaim skip bad inflight file %s", fn)
                continue
            leased_until = item.get("leased_until", 0)
            # 无 lease 字段 (旧数据) 且超过 1 小时视为孤儿, 回收; 有 lease 且过期才回收.
            is_orphan = "leased_until" not in item and now - item.get("created_at", 0) > 3600
            is_expired = leased_until and now > leased_until
            if not (is_orphan or is_expired):
                continue
            src = path
            dst = os.path.join(self.PENDING_DIR, fn)
            try:
                os.replace(src, dst)
                reclaimed += 1
                logger.warning("reclaim expired lease id=%s leased_by=%s until=%d now=%d", item.get("id"), item.get("leased_by"), leased_until, now)
            except OSError as e:
                logger.error("reclaim move failed id=%s: %s", item.get("id"), e)
        return reclaimed

    def _archive(self, item_id, status, vid=""):
        if not item_id:
            logger.error("archive empty item_id")
            return json.dumps({"error": "empty item_id"})
        # 优先从 in-flight 取 (dequeue 移过来的); 回退 pending (兼容旧数据).
        src_inflight = os.path.join(self.INFLIGHT_DIR, f"{item_id}.json")
        src_pending = os.path.join(self.PENDING_DIR, f"{item_id}.json")
        dst_dir = self.PUBLISHED_DIR if status == "published" else self.FAILED_DIR
        # 修 TOCTOU: exists 检查 + open + remove 全移入锁内. 旧实现锁外检查 exists,
        # 并发 archive 同 item 时第二个 os.remove(src) FileNotFoundError 冒泡 (cron 不并发 archive 同 item, 低危但应修).
        with self._lock():
            src = src_inflight if os.path.exists(src_inflight) else src_pending
            if not os.path.exists(src):
                logger.error("archive not found id=%s", item_id)
                return json.dumps({"error": "item not found"})
            try:
                with open(src, encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError) as e:
                logger.error("archive read failed id=%s: %s", item_id, e)
                return json.dumps({"error": f"read failed: {e}"})
            # lease 归属校验 (审计 H1): inflight item 带 leased_by, 仅归属本节点或 lease 过期才允许归档.
            # 别节点持未过期 lease 仍在发布中 -> 拒绝归档防双发. 过期 lease 视崩溃恢复, 允许归档避免卡死.
            # 单节点 NODE_ID="" 时 leased_by 同为 "" 恒通过, 无副作用.
            leased_by = item.get("leased_by")
            leased_until = item.get("leased_until", 0)
            now = int(time.time())
            if leased_by is not None and leased_by != NODE_ID and now <= leased_until:
                logger.error("archive reject lease mismatch id=%s owned_by=%s this_node=%s until=%d", item_id, leased_by, NODE_ID, leased_until)
                return json.dumps({"error": f"lease held by {leased_by} until {leased_until}"}, ensure_ascii=False)
            if leased_by is not None and leased_by != NODE_ID and now > leased_until:
                logger.warning("archive reclaim expired lease id=%s was_owned_by=%s expired=%d", item_id, leased_by, leased_until)
            item["status"] = status
            item["archived_at"] = int(time.time())
            if vid:
                item["vid"] = vid
            dst = os.path.join(dst_dir, f"{item_id}.json")
            tmp = dst + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(item, f, ensure_ascii=False)
            os.replace(tmp, dst)
            try:
                os.remove(src)
            except FileNotFoundError:
                pass
            pending = len(self._list_pending())
        logger.info("archive ok id=%s status=%s vid=%s pending=%d", item_id, status, vid, pending)
        return json.dumps({"status": "archived", "item_id": item_id, "vid": vid, "pending": pending}, ensure_ascii=False)

    def _count(self):
        with self._lock():
            pending = len(self._list_pending())
            inflight = len(self._list_dir(self.INFLIGHT_DIR))
            published = len(self._list_dir(self.PUBLISHED_DIR))
            failed = len(self._list_dir(self.FAILED_DIR))
        logger.info("count pending=%d inflight=%d published=%d failed=%d", pending, inflight, published, failed)
        return json.dumps({"pending": pending, "inflight": inflight, "published": published, "failed": failed, "has_stock": "true" if pending > 0 else "false"}, ensure_ascii=False)

    def _list_pending(self):
        return self._list_dir(self.PENDING_DIR)

    @staticmethod
    def _list_dir(d):
        if not os.path.isdir(d):
            return []
        out = []
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (ValueError, OSError):
                logger.warning("skip bad queue file %s", fn)
        out.sort(key=lambda x: x.get("created_at", 0))
        return out
