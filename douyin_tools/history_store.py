import datetime
import json
import logging
import os

from .config import OPS_DATA_DIR

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(OPS_DATA_DIR, "stats_history.jsonl")

_MAX_HISTORY_PER_VID_DEFAULT = 40


def load_history(path=None):
    rows = []
    p = path or HISTORY_FILE
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        return rows
    except OSError as exc:
        logger.warning("load_history read failed path=%s err=%s", p, exc)
        return rows
    return rows


def _parse_snap_time(raw):
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def latest_per_vid(rows):
    latest = {}
    latest_ts = {}
    for r in rows:
        vid = r.get("vid", "")
        if not vid:
            continue
        sa = r.get("snapshot_at", "")
        dt = _parse_snap_time(sa)
        ts = dt.timestamp() if dt else 0
        cur = latest.get(vid)
        if cur is None or ts >= latest_ts.get(vid, -1):
            latest[vid] = r
            latest_ts[vid] = ts
    return latest


def append_history(stats, date, path=None):
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
    p = path or HISTORY_FILE
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    logger.info("append_history date=%s items=%d plays=%d", date, len(items), snap["total_plays"])


def prune_history(max_per_vid=_MAX_HISTORY_PER_VID_DEFAULT, prune_threshold=None, path=None):
    p = path or HISTORY_FILE
    cap = int(max_per_vid or _MAX_HISTORY_PER_VID_DEFAULT)
    threshold = int(prune_threshold if prune_threshold is not None else cap * 200)
    if not os.path.exists(p):
        return 0
    rows = load_history(p)
    if len(rows) < threshold:
        return 0
    by_vid = {}
    for r in rows:
        vid = r.get("vid", "")
        if not vid:
            continue
        by_vid.setdefault(vid, []).append(r)
    kept = []
    for vid, lst in by_vid.items():
        kept.extend(lst[-cap:])
    tmp_path = p + ".pruned"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_path, p)
    dropped = len(rows) - len(kept)
    logger.info("prune_history rows=%d kept=%d dropped=%d per_vid_cap=%d", len(rows), len(kept), dropped, cap)
    return dropped
