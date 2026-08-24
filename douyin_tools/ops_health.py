import json
import logging
import os
import time

from tools.base import BaseTool

from .config import OPS_DATA_DIR, QUEUE_DIR, AGENT_STUDIO_SOCKET, ensure_dirs

logger = logging.getLogger(__name__)

PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
INFLIGHT_DIR = os.path.join(QUEUE_DIR, "inflight")
PUBLISHED_DIR = os.path.join(QUEUE_DIR, "published")
FAILED_DIR = os.path.join(QUEUE_DIR, "failed")
PATTERNS_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")
BLACKLIST_FILE = os.path.join(OPS_DATA_DIR, "topic_blacklist.json")
REPLIED_FILE = os.path.join(OPS_DATA_DIR, "replied.json")
HISTORY_FILE = os.path.join(OPS_DATA_DIR, "stats_history.jsonl")

# 健康阈值: cron 失败率超 30% 告警, 关键文件超过 N 小时未更新告警.
CRON_FAIL_RATE_THRESHOLD = 0.30
EVOLVE_STALE_HOURS = 30
REPLIED_STALE_HOURS = 48
LOW_PENDING_THRESHOLD = 3


class OpsHealthTool(BaseTool):
    name = "ops_health"
    description = "运营健康监控: 检查 cron 失败率 + 库存计数 + winning_patterns/blacklist/replied/stats mtime + 发布队列 vid 回填率, 输出告警清单供每日巡检"
    parameters = {
        "check_cron": {"type": "boolean", "description": "是否查 cron 执行记录(需 daemon 在线), 默认 True"},
        "days": {"type": "integer", "description": "检查最近 N 天 cron 执行, 默认 1"},
    }

    async def execute(self, check_cron=True, days=1, **_):
        ensure_dirs()
        alerts = []
        summary = {}

        summary["queue"] = self._queue_counts()
        for a in self._queue_alerts(summary["queue"]):
            alerts.append(a)

        files = self._file_mtimes()
        summary["files"] = files
        for a in self._file_alerts(files):
            alerts.append(a)

        if check_cron:
            try:
                cron = await self._cron_health(days)
                summary["cron"] = cron
                for a in self._cron_alerts(cron):
                    alerts.append(a)
            except Exception as e:
                logger.warning("ops_health cron check failed: %s", e)
                alerts.append(f"cron_check_unavailable: {e}")
                summary["cron"] = {"error": str(e)[:200]}

        summary["vid_backfill"] = self._vid_backfill_rate()
        healthy = len(alerts) == 0
        out = {
            "status": "healthy" if healthy else "degraded",
            "alerts": alerts,
            "alert_count": len(alerts),
            "checked_at": int(time.time()),
            "summary": summary,
        }
        logger.info("ops_health status=%s alerts=%d", out["status"], len(alerts))
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _count_dir(d):
        if not os.path.isdir(d):
            return 0
        return sum(1 for fn in os.listdir(d) if fn.endswith(".json"))

    def _queue_counts(self):
        return {
            "pending": self._count_dir(PENDING_DIR),
            "inflight": self._count_dir(INFLIGHT_DIR),
            "published": self._count_dir(PUBLISHED_DIR),
            "failed": self._count_dir(FAILED_DIR),
        }

    @staticmethod
    def _queue_alerts(q):
        out = []
        if q["pending"] < LOW_PENDING_THRESHOLD:
            out.append(f"low_stock: pending={q['pending']} 库存不足, 补货链需检查")
        if q["inflight"] > 0:
            out.append(f"stuck_inflight: inflight={q['inflight']} 有卡住未归档项")
        if q["failed"] > 0:
            out.append(f"failed_items: failed={q['failed']} 有失败视频待排查")
        return out

    @staticmethod
    def _file_mtimes():
        out = {}
        for name, path in (
            ("winning_patterns", PATTERNS_FILE),
            ("topic_blacklist", BLACKLIST_FILE),
            ("replied", REPLIED_FILE),
            ("stats_history", HISTORY_FILE),
        ):
            if os.path.exists(path):
                out[name] = {"path": path, "mtime": int(os.path.getmtime(path)), "exists": True}
            else:
                out[name] = {"path": path, "exists": False}
        return out

    @staticmethod
    def _file_alerts(files):
        out = []
        now = int(time.time())
        evolved = files.get("winning_patterns", {})
        if not evolved.get("exists"):
            out.append("evolve_never_run: winning_patterns.json 不存在, 进化闭环从未产出")
        elif now - evolved["mtime"] > EVOLVE_STALE_HOURS * 3600:
            out.append(f"evolve_stale: winning_patterns.json 超过 {EVOLVE_STALE_HOURS}h 未更新, 进化闭环停摆")
        replied = files.get("replied", {})
        if not replied.get("exists"):
            out.append("comment_never_run: replied.json 不存在, 评论闭环从未执行")
        elif now - replied["mtime"] > REPLIED_STALE_HOURS * 3600:
            out.append(f"comment_stale: replied.json 超过 {REPLIED_STALE_HOURS}h 未更新, 评论闭环停摆")
        bl = files.get("topic_blacklist", {})
        if not bl.get("exists"):
            out.append("self_repair_never_run: topic_blacklist.json 不存在, 差片自我修复从未落盘")
        return out

    async def _cron_health(self, days):
        from agent_runtime.sdk.client import AgentClient
        client = AgentClient(socket_path=AGENT_STUDIO_SOCKET)
        res = await client.call("cron.list", {})
        if res.get("error"):
            raise RuntimeError(f"cron.list failed: {res['error']}")
        jobs = res.get("jobs", [])
        total_exec = 0
        failed_exec = 0
        per_job = []
        cutoff = int(time.time()) - days * 86400
        for job in jobs:
            jid = job.get("id", "")
            runs = []
            try:
                er = await client.call("cron.list_executions", {"job_id": jid, "limit": 50})
                runs = er.get("executions", []) or []
            except Exception as e:
                logger.warning("cron.list_executions failed job=%s: %s", jid, e)
            recent = [r for r in runs if int(r.get("executed_at", 0) or 0) >= cutoff]
            j_total = len(recent)
            j_fail = sum(1 for r in recent if (r.get("status") or "") != "success")
            total_exec += j_total
            failed_exec += j_fail
            per_job.append({
                "id": jid,
                "expression": job.get("expression", ""),
                "enabled": job.get("enabled", True),
                "graph_id": job.get("graph_id", ""),
                "recent_total": j_total,
                "recent_failed": j_fail,
            })
        fail_rate = (failed_exec / total_exec) if total_exec else 0.0
        return {
            "job_count": len(jobs),
            "recent_total": total_exec,
            "recent_failed": failed_exec,
            "fail_rate": round(fail_rate, 3),
            "per_job": per_job,
        }

    @staticmethod
    def _cron_alerts(cron):
        out = []
        if cron.get("job_count", 0) < 7:
            out.append(f"cron_jobs_missing: 期望 7 个 cron job, 实际 {cron.get('job_count', 0)}")
        for j in cron.get("per_job", []):
            if not j.get("enabled"):
                out.append(f"cron_disabled: {j['id']} 被禁用")
        if cron.get("fail_rate", 0) > CRON_FAIL_RATE_THRESHOLD:
            out.append(f"cron_high_fail_rate: 失败率 {cron['fail_rate']} 超阈值 {CRON_FAIL_RATE_THRESHOLD}")
        for j in cron.get("per_job", []):
            if j["recent_total"] == 0:
                out.append(f"cron_no_runs: {j['id']} 最近无执行记录 (可能 graph_id 漂移或未触发)")
        return out

    @staticmethod
    def _vid_backfill_rate():
        if not os.path.isdir(PUBLISHED_DIR):
            return {"rate": 0.0, "total": 0, "with_vid": 0}
        total = 0
        with_vid = 0
        for fn in os.listdir(PUBLISHED_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(PUBLISHED_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                continue
            total += 1
            if item.get("vid"):
                with_vid += 1
        rate = (with_vid / total) if total else 0.0
        return {"rate": round(rate, 3), "total": total, "with_vid": with_vid}
