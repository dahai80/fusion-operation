import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# M3-5: queue 生命周期 SQLite store. DB 权威, 文件目录 (pending/inflight/published/failed)
# 降为投影 (projection), 消费者仍读 JSON 文件. 审计 A1: 纯文件无事务/索引/备份,
# 崩溃留半截 JSON 致队列卡死. SQLite WAL + busy_timeout + RLock 串行化 (对齐
# fusion-agent-studio persistence.py:64-68 / task_store.py:267 模式).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    video_path TEXT,
    script_json TEXT,
    topic TEXT,
    title TEXT,
    description TEXT,
    hook_variant TEXT,
    vid TEXT,
    leased_by TEXT,
    leased_until INTEGER,
    created_at INTEGER NOT NULL,
    archived_at INTEGER,
    requeued_at INTEGER,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(status);
CREATE INDEX IF NOT EXISTS idx_queue_created ON queue_items(created_at);
CREATE INDEX IF NOT EXISTS idx_queue_leased_until ON queue_items(leased_until);
CREATE INDEX IF NOT EXISTS idx_queue_archived ON queue_items(archived_at);
"""

# status 枚举与文件投影目录一一对应.
STATUS_PENDING = "pending"
STATUS_INFLIGHT = "inflight"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
_ALL_STATUSES = (STATUS_PENDING, STATUS_INFLIGHT, STATUS_PUBLISHED, STATUS_FAILED)

# 孤儿判定: 无 lease 字段的旧数据 age 超此阈值视为崩溃遗留, 回收 (对齐 queue_ops._reclaim_expired).
_ORPHAN_AGE_SEC = 3600


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    # script_json / payload 存为 json 字符串; 消费者按需 json.loads.
    return d


class QueueStore:
    # 单连接 + RLock 串行化: 同进程工具调用足够, WAL 让读不堵写.
    # 跨进程并发靠 SQLite 自身文件锁 + busy_timeout, 不再需要 fcntl.flock.
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = None
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("queue_store init db=%s", self.db_path)

    def enqueue(self, item):
        # INSERT OR IGNORE: id (ts_title_uuid) 冲突时保留旧行, 幂等.
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO queue_items
                   (id, status, video_path, script_json, topic, title, description,
                    hook_variant, vid, leased_by, leased_until, created_at,
                    archived_at, requeued_at, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["id"],
                    STATUS_PENDING,
                    item.get("video_path", ""),
                    _dumps(item.get("script_json")),
                    item.get("topic", ""),
                    item.get("title", ""),
                    item.get("description", ""),
                    item.get("hook_variant", ""),
                    item.get("vid", ""),
                    None,
                    None,
                    item.get("created_at", int(time.time())),
                    None,
                    None,
                    _dumps(item.get("payload")) if item.get("payload") is not None else None,
                ),
            )
            self._conn.commit()
        logger.info("queue_store enqueue id=%s", item["id"])
        return item["id"]

    def reclaim_expired(self, now=None):
        # inflight 中 lease 过期 或 孤儿 (无 lease + age>_ORPHAN_AGE_SEC) 移回 pending.
        # 保留 leased_by/until 留痕可审计 (与 queue_ops._reclaim_expired 一致).
        now = int(time.time()) if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """SELECT id, leased_until, created_at FROM queue_items
                   WHERE status=?
                     AND (leased_until IS NOT NULL AND leased_until < ?
                          OR (leased_until IS NULL AND created_at < ?))""",
                (STATUS_INFLIGHT, now, now - _ORPHAN_AGE_SEC),
            )
            rows = cur.fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE queue_items SET status=? WHERE id=?",
                    (STATUS_PENDING, r["id"]),
                )
            self._conn.commit()
        if rows:
            logger.warning("queue_store reclaimed %d expired-lease inflight items", len(rows))
        return len(rows)

    def dequeue(self, lease_ttl, node_id="", now=None):
        # 原子取队首: SELECT oldest pending → UPDATE inflight + lease. RLock 内单事务.
        # 跨进程并发靠 SQLite 行锁, 两个进程同时 SELECT 同一行时第二个 UPDATE 仍只影响 1 行
        # (status 已变 inflight, WHERE status=pending 不再命中).
        now = int(time.time()) if now is None else now
        self.reclaim_expired(now)
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM queue_items WHERE status=?
                   ORDER BY created_at ASC, id ASC LIMIT 1""",
                (STATUS_PENDING,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            leased_until = now + int(lease_ttl)
            self._conn.execute(
                """UPDATE queue_items
                   SET status=?, leased_by=?, leased_until=?
                   WHERE id=? AND status=?""",
                (STATUS_INFLIGHT, node_id, leased_until, row["id"], STATUS_PENDING),
            )
            self._conn.commit()
            d = _row_to_dict(row)
            d["status"] = STATUS_INFLIGHT
            d["leased_by"] = node_id
            d["leased_until"] = leased_until
        logger.info("queue_store dequeue id=%s leased_by=%s until=%d", d["id"], node_id, leased_until)
        return d

    def requeue(self, item_id, now=None):
        # inflight → pending, strip lease, 加 requeued_at (显式失败重排, 消除回收路径依赖).
        now = int(time.time()) if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM queue_items WHERE id=? AND status=?",
                (item_id, STATUS_INFLIGHT),
            )
            if cur.fetchone() is None:
                logger.error("queue_store requeue not inflight id=%s", item_id)
                return False
            self._conn.execute(
                """UPDATE queue_items
                   SET status=?, leased_by=NULL, leased_until=?, requeued_at=?
                   WHERE id=?""",
                (STATUS_PENDING, None, now, item_id),
            )
            self._conn.commit()
        logger.warning("queue_store requeue id=%s back to pending", item_id)
        return True

    def archive(self, item_id, status, vid="", node_id="", now=None):
        # lease 归属校验 (审计 H1): inflight item 带 leased_by, 仅归属本节点或 lease 过期允许归档.
        # 别节点持未过期 lease → 拒绝防双发. 过期 → 崩溃恢复允许归档避免卡死.
        now = int(time.time()) if now is None else now
        if status not in (STATUS_PUBLISHED, STATUS_FAILED):
            logger.error("queue_store archive bad status=%s", status)
            return {"error": f"bad status {status}"}
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM queue_items WHERE id=?",
                (item_id,),
            )
            row = cur.fetchone()
            if row is None:
                logger.error("queue_store archive not found id=%s", item_id)
                return {"error": "item not found"}
            d = _row_to_dict(row)
            leased_by = d.get("leased_by")
            leased_until = d.get("leased_until") or 0
            if leased_by is not None and leased_by != node_id and now <= leased_until:
                logger.error(
                    "queue_store archive reject lease mismatch id=%s owned_by=%s this=%s until=%d",
                    item_id, leased_by, node_id, leased_until,
                )
                return {"error": f"lease held by {leased_by} until {leased_until}"}
            if leased_by is not None and leased_by != node_id and now > leased_until:
                logger.warning(
                    "queue_store archive reclaim expired lease id=%s was_owned=%s",
                    item_id, leased_by,
                )
            self._conn.execute(
                """UPDATE queue_items
                   SET status=?, archived_at=?, vid=?, leased_by=NULL, leased_until=NULL
                   WHERE id=?""",
                (status, now, vid or d.get("vid", ""), item_id),
            )
            self._conn.commit()
            d["status"] = status
            d["archived_at"] = now
            if vid:
                d["vid"] = vid
        logger.info("queue_store archive id=%s status=%s vid=%s", item_id, status, vid)
        return d

    def count(self):
        with self._lock:
            cur = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM queue_items GROUP BY status"
            )
            counts = {s: 0 for s in _ALL_STATUSES}
            for r in cur.fetchall():
                counts[r["status"]] = r["n"]
        return counts

    def list_by_status(self, status, limit=0):
        with self._lock:
            if limit > 0:
                cur = self._conn.execute(
                    "SELECT * FROM queue_items WHERE status=? ORDER BY created_at ASC LIMIT ?",
                    (status, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM queue_items WHERE status=? ORDER BY created_at ASC",
                    (status,),
                )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def list_all(self):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM queue_items")
            return [_row_to_dict(r) for r in cur.fetchall()]

    def get(self, item_id):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM queue_items WHERE id=?", (item_id,))
            return _row_to_dict(cur.fetchone())

    def list_inflight_stale(self, threshold_sec, now=None):
        # lease 过期超 threshold_sec 的 inflight 项 = reclaim 未跑 (dequeue 路径断/daemon 死锁).
        now = int(time.time()) if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM queue_items
                   WHERE status=? AND leased_until IS NOT NULL AND ? - leased_until > ?""",
                (STATUS_INFLIGHT, now, threshold_sec),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def list_archived_older_than(self, days, now=None):
        now = int(time.time()) if now is None else now
        cutoff = now - int(days) * 86400
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM queue_items
                   WHERE status IN (?,?) AND archived_at IS NOT NULL AND archived_at < ?""",
                (STATUS_PUBLISHED, STATUS_FAILED, cutoff),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def prune_older_than(self, days, now=None):
        # 删 published/failed 超 N 天的归档项, 返回被删 rows (含 id 供删投影文件).
        deleted = self.list_archived_older_than(days, now)
        if not deleted:
            return []
        with self._lock:
            for d in deleted:
                self._conn.execute("DELETE FROM queue_items WHERE id=?", (d["id"],))
            self._conn.commit()
        logger.info("queue_store pruned %d archived items older than %d days", len(deleted), days)
        return deleted

    def set_leased_until(self, item_id, leased_until):
        # 测试辅助: 模拟崩溃把 lease 调到过去, 触发 reclaim 路径.
        with self._lock:
            self._conn.execute(
                "UPDATE queue_items SET leased_until=? WHERE id=?",
                (leased_until, item_id),
            )
            self._conn.commit()
        logger.info("queue_store set_leased_until id=%s until=%s (test/crash-sim)", item_id, leased_until)

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def _dumps(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _item_to_file_dict(d):
    # DB row → 投影文件 JSON (消费者期望的字段集, 与旧 queue_ops 文件格式一致).
    out = {
        "id": d.get("id", ""),
        "video_path": d.get("video_path", ""),
        "topic": d.get("topic", ""),
        "title": d.get("title", ""),
        "description": d.get("description", ""),
        "hook_variant": d.get("hook_variant", ""),
        "status": d.get("status", ""),
        "created_at": d.get("created_at", 0),
    }
    # script_json: 还原为 dict (消费者 topic_gen/perf_analyze 期望 dict, 非 json 字符串).
    raw = d.get("script_json")
    if raw:
        try:
            out["script_json"] = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            out["script_json"] = raw
    else:
        out["script_json"] = {}
    if d.get("vid") is not None:
        out["vid"] = d.get("vid")
    if d.get("leased_by") is not None:
        out["leased_by"] = d.get("leased_by")
    if d.get("leased_until") is not None:
        out["leased_until"] = d.get("leased_until")
    if d.get("archived_at") is not None:
        out["archived_at"] = d.get("archived_at")
    if d.get("requeued_at") is not None:
        out["requeued_at"] = d.get("requeued_at")
    return out


def _safe_name(item_id):
    import re
    return re.sub(r"[^\w\-]", "_", str(item_id), flags=re.UNICODE)[:120]


_STATUS_DIR = {
    STATUS_PENDING: "pending",
    STATUS_INFLIGHT: "inflight",
    STATUS_PUBLISHED: "published",
    STATUS_FAILED: "failed",
}


def _status_dir_path(status, dirs):
    name = _STATUS_DIR.get(status)
    if not name:
        return None
    return dirs.get(name)


def write_projection(d, dirs):
    # 把 DB row 投影为对应状态目录的 JSON 文件 (atomic tmp+os.replace).
    # 同时移除其他状态目录的同 id 残留文件 (状态迁移时旧目录清场).
    status = d.get("status")
    target = _status_dir_path(status, dirs)
    safe = _safe_name(d.get("id", ""))
    fname = f"{safe}.json"
    # 清其他目录残留.
    for s in _ALL_STATUSES:
        if s == status:
            continue
        p = os.path.join(_status_dir_path(s, dirs), fname)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning("projection remove stale %s: %s", p, e)
    if not target:
        return
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, fname)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_item_to_file_dict(d), f, ensure_ascii=False)
    os.replace(tmp, path)


def remove_projection(item_id, dirs):
    # 从所有状态目录移除该 item 的投影文件 (DB 删行后清场).
    safe = _safe_name(item_id)
    fname = f"{safe}.json"
    for s in _ALL_STATUSES:
        p = os.path.join(_status_dir_path(s, dirs), fname)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                logger.warning("remove_projection %s: %s", p, e)


def migrate_from_files(store, dirs):
    # 一次性迁移: DB 空 + 文件存在 → import 现有文件 items 入 DB (idempotent by id PK).
    if store.count().get(STATUS_PENDING, 0) or any(
        store.list_by_status(s) for s in (STATUS_INFLIGHT, STATUS_PUBLISHED, STATUS_FAILED)
    ):
        logger.info("migrate_from_files: DB non-empty, skip import")
        return 0
    imported = 0
    for status, dirname in _STATUS_DIR.items():
        d = dirs.get(dirname)
        if not d or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError) as e:
                logger.warning("migrate skip bad file %s/%s: %s", dirname, fn, e)
                continue
            if not item.get("id"):
                continue
            # 按 DB 列写入; status 以目录为准 (覆盖文件内 status, 防漂移).
            store.enqueue({**item, "status": STATUS_PENDING, "created_at": item.get("created_at", int(time.time()))})
            # 修正状态: enqueue 强制 pending, 非目录状态需 UPDATE.
            if status != STATUS_PENDING:
                with store._lock:
                    store._conn.execute(
                        "UPDATE queue_items SET status=? WHERE id=?",
                        (status, item["id"]),
                    )
                    store._conn.commit()
            imported += 1
    logger.info("migrate_from_files imported %d items from dirs", imported)
    return imported


def reconcile_to_files(store, dirs):
    # 投影一致性检查 (风险行 line 548): DB 权威, 文件投影. drift → 修文件 + 返回 mismatch.
    # 缺文件 → 写; 多余文件 (DB 无对应行) → 删; 状态目录错位 → 移到正确目录.
    mismatches = []
    db_ids_by_status = {}
    all_rows = store.list_all()
    db_ids = set()
    for d in all_rows:
        db_ids.add(d["id"])
        db_ids_by_status.setdefault(d.get("status"), set()).add(d["id"])
    # 1. 缺文件: DB 有行但目录无文件 → 写投影.
    for status in db_ids_by_status:
        target = _status_dir_path(status, dirs)
        if not target:
            continue
        for d in all_rows:
            if d.get("status") == status:
                safe = _safe_name(d["id"])
                p = os.path.join(target, f"{safe}.json")
                if not os.path.exists(p):
                    write_projection(d, dirs)
                    mismatches.append({"id": d["id"], "kind": "missing_file", "status": status})
    # 2. 多余/错位文件: 各目录文件, DB 无对应行 → 删; DB 有但状态不匹配 → 删错位 (正确目录由 step1 补).
    for status, dirname in _STATUS_DIR.items():
        d = dirs.get(dirname)
        if not d or not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            item_id = fn[:-5]
            if item_id not in db_ids:
                try:
                    os.remove(os.path.join(d, fn))
                    mismatches.append({"id": item_id, "kind": "orphan_file", "dir": dirname})
                except OSError as e:
                    logger.warning("reconcile remove orphan %s/%s: %s", dirname, fn, e)
            else:
                # 行存在但状态目录不匹配 → 删错位文件 (正确目录 step1 已写/将写).
                if item_id not in db_ids_by_status.get(status, set()):
                    try:
                        os.remove(os.path.join(d, fn))
                        mismatches.append({"id": item_id, "kind": "misplaced_file", "dir": dirname})
                    except OSError as e:
                        logger.warning("reconcile remove misplaced %s/%s: %s", dirname, fn, e)
    if mismatches:
        logger.warning("reconcile drift=%d mismatches=%s", len(mismatches), mismatches[:10])
    return len(mismatches), mismatches
