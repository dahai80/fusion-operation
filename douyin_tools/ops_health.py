import asyncio
import json
import logging
import os
import time

from tools.base import BaseTool

from .config import OPS_DATA_DIR, QUEUE_DIR, AGENT_STUDIO_SOCKET, ensure_dirs, get_json_config

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
# lease TTL: 须与 queue_ops.LEASE_TTL 同源 (审计 0908 F2 ④ stale-inflight 告警阈值 = 2×TTL).
# master queue_ops 定义为本地常量读 DOUYIN_LEASE_TTL env default 300; 此处镜像同源读法.
# WIP 已上调 default 至 1800 覆盖最坏发布耗时 (审计 P0-1); 此处取 1800 保一致.
LEASE_TTL = int(os.environ.get("DOUYIN_LEASE_TTL", "1800"))
# 审计 P2-7: 期望 cron job 数硬编码 7, 实际 9 (5 发布+2 评论+1 进化+1 修复).
# 改从 douyin_config.json scheduler.cron_jobs 声明式读, 未声明回退 9.
try:
    _cfg_cron = get_json_config().get("scheduler", {}).get("cron_jobs", 9)
    EXPECTED_CRON_JOBS = int(_cfg_cron) if int(_cfg_cron) > 0 else 9
except (TypeError, ValueError):
    EXPECTED_CRON_JOBS = 9


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
    def _safe_int(v, default=0):
        # 审计 P3-7: int(None/"") 崩, 非数字字符串也崩. 统一安全转 int.
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

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
        # 审计 0908 F2 ④: inflight 超 2×LEASE_TTL = 发布进程崩溃/卡死未归档, 重复发布窗口已打开.
        # lease 到期 _reclaim_expired 会移回 pending 重发, 但若 inflight 项 age 远超 2×TTL 仍在,
        # 说明 reclaim 本身没跑 (dequeue 路径未触发) 或 daemon 死锁. 告警人工介入.
        stale = OpsHealthTool._stale_inflight_count()
        stale_threshold = 2 * LEASE_TTL
        if stale > 0:
            out.append(
                f"stale_inflight_over_2x_ttl: stale={stale} threshold={stale_threshold}s "
                f"发布进程疑似崩溃/卡死, 重复发布窗口已打开, 需人工介入"
            )
        if q["failed"] > 0:
            out.append(f"failed_items: failed={q['failed']} 有失败视频待排查")
        return out

    @staticmethod
    def _stale_inflight_count():
        # 扫 inflight, age (now - leased_until) 超 2×LEASE_TTL 的项 = lease 早过期但未被 reclaim.
        # 正常 reclaim 在下轮 dequeue 触发; 持续超时说明 daemon 不健康或 dequeue 路径断.
        if not os.path.isdir(INFLIGHT_DIR):
            return 0
        now = int(time.time())
        threshold = 2 * LEASE_TTL
        stale = 0
        for fn in os.listdir(INFLIGHT_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(INFLIGHT_DIR, fn), encoding="utf-8") as f:
                    item = json.load(f)
            except (ValueError, OSError):
                stale += 1
                continue
            leased_until = item.get("leased_until", 0)
            if leased_until and now - leased_until > threshold:
                stale += 1
        return stale

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
        # 审计 P2-9: client.call 无 timeout, daemon hang (非 down) -> 无限阻塞 -> ops_health 卡死无法报警.
        # 加 asyncio.wait_for 10s 预算.
        try:
            res = await asyncio.wait_for(client.call("cron.list", {}), timeout=10)
        except asyncio.TimeoutError:
            raise RuntimeError("cron.list timed out >10s (daemon hang?)")
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
                er = await asyncio.wait_for(
                    client.call("cron.list_executions", {"job_id": jid, "limit": 50}), timeout=10
                )
                runs = er.get("executions", []) or []
            except (Exception, asyncio.TimeoutError) as e:
                logger.warning("cron.list_executions failed job=%s: %s", jid, e)
            # 审计 P3-7: int(executed_at) 假定 int, 非数字/None 崩 -> 用 _safe_int 守卫.
            recent = [r for r in runs if self._safe_int(r.get("executed_at")) >= cutoff]
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
        # 审计 P2-7: 阈值硬编码 7, 实际 9 job. 改读 EXPECTED_CRON_JOBS (声明式配置层).
        expected = EXPECTED_CRON_JOBS
        if cron.get("job_count", 0) < expected:
            out.append(f"cron_jobs_missing: 期望 {expected} 个 cron job, 实际 {cron.get('job_count', 0)}")
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
