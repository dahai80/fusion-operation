#!/bin/bash
# 统一部署: sync_all → daemon restart → 校验闭环. 任一步失败即 abort 不静默继续 (修 M3).
# 顺序: venv_deps → plugins → agent_config → graphs(cron 回填) → daemon 重启 → RPC 校验.
# 校验: tool.list ≥ 20 + failed_plugins 空 + cron.list ≥ 7 job. 失败 exit 1.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="/Users/dahai/fusion/.venv"
PYTHON="${VENV}/bin/python"
DAEMON_SH="/Users/dahai/fusion/fusion-agent-studio/start.sh"
MIN_TOOLS=20
MIN_CRON_JOBS=7

log() { echo "[deploy] $(date +%H:%M:%S) $*"; }
fail() { echo "[deploy][FAIL] $*" >&2; exit 1; }

[ -x "$PYTHON" ] || fail "venv python 不存在: $PYTHON"
[ -x "$DAEMON_SH" ] || fail "daemon start.sh 不存在: $DAEMON_SH"

log "1/5 sync_venv_deps (plugin venv 依赖对齐)"
"$PYTHON" sync_venv_deps.py || fail "sync_venv_deps 失败"

log "2/5 sync_plugins (douyin plugin shim 注入 daemon)"
"$PYTHON" sync_plugins.py || fail "sync_plugins 失败"

log "3/5 sync_agent_config (tool config 注入智能体包)"
"$PYTHON" sync_agent_config.py || fail "sync_agent_config 失败"

log "4/5 sync_graphs (graph 进 store + cron job graph_id 回填)"
"$PYTHON" sync_graphs.py || fail "sync_graphs 失败"

# 审计 0908 F2 ③: daemon 重启会杀掉 in-flight graph 执行. 若部署发生在孤儿执行期间 (publish
# 上传中), 重启中断发布 -> item 卡 inflight -> lease 到期 reclaim -> 重发 (重复发布).
# 部署门禁: inflight 非空时禁止重启, 延迟部署等 inflight 清空 (archive/requeue 完成).
log "4.5/5 部署门禁: 检查 inflight (非空 = 有发布进行中, 延迟部署)"
INFLIGHT_CHECK="$("$PYTHON" -c "
import os, sys
sys.path.insert(0, '.')
from douyin_tools.config import QUEUE_DIR
inflight = os.path.join(QUEUE_DIR, 'inflight')
n = sum(1 for f in os.listdir(inflight) if f.endswith('.json')) if os.path.isdir(inflight) else 0
print(n)
" 2>/dev/null || echo "ERR")"
if [ "$INFLIGHT_CHECK" = "ERR" ]; then
    fail "inflight 检查脚本异常, 中止部署 (不盲重启)"
fi
if [ "$INFLIGHT_CHECK" != "0" ]; then
    fail "inflight 非空 (count=$INFLIGHT_CHECK): 有发布进行中, daemon 重启会中断致重复发布. 等 inflight 清空后重新部署"
fi
log "inflight=0, 安全重启"

log "5/5 daemon 重启 (start.sh 统一 unset client key env, 走 settings.json 回退)"
if launchctl list 2>/dev/null | grep -q "com.fusion-agent-studio.server"; then
    log "launchd 管控中, uninstall+install 重载 plugin"
    "$DAEMON_SH" uninstall-launchd >/dev/null 2>&1 || true
    "$DAEMON_SH" install-launchd || fail "daemon install-launchd 失败"
else
    "$DAEMON_SH" restart || fail "daemon restart 失败"
fi
sleep 3

log "RPC 校验: tool.list + cron.list"
VERIFY_SCRIPT="$(mktemp -t deploy_verify.XXXXXX).py"
trap 'rm -f "$VERIFY_SCRIPT" "$VERIFY_SCRIPT"' EXIT
cat > "$VERIFY_SCRIPT" <<'PYEOF'
import asyncio, json, sys
from agent_runtime.sdk.client import AgentClient
from douyin_tools.config import AGENT_STUDIO_SOCKET

async def main():
    c = AgentClient(socket_path=AGENT_STUDIO_SOCKET)
    for _ in range(20):
        try:
            s = await c.call("daemon.status", {})
            if s.get("status") == "running":
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    else:
        print("daemon_not_running", file=sys.stderr)
        sys.exit(1)
    tl = await c.call("tool.list", {})
    tools = tl.get("tools", [])
    failed = tl.get("failed_plugins", {}) or {}
    cr = await c.call("cron.list", {})
    jobs = cr.get("jobs", [])
    tool_names = [t.get("name", "") for t in tools]
    has_publish_scheduler = "publish_scheduler" in tool_names
    has_ops_health = "ops_health" in tool_names
    ok = (len(tools) >= 20 and not failed and len(jobs) >= 7
          and has_publish_scheduler and has_ops_health)
    print(json.dumps({
        "tool_count": len(tools),
        "failed_plugins": failed,
        "cron_jobs": len(jobs),
        "has_publish_scheduler": has_publish_scheduler,
        "has_ops_health": has_ops_health,
    }, ensure_ascii=False))
    sys.exit(0 if ok else 2)

asyncio.run(main())
PYEOF
VERIFY=$("$PYTHON" "$VERIFY_SCRIPT" 2>&1) || true
rm -f "$VERIFY_SCRIPT"
echo "$VERIFY" | tail -1 | grep -q '"tool_count"' || fail "校验未返回结果: $VERIFY"
RESULT=$(echo "$VERIFY" | tail -1)
TOOL_COUNT=$(echo "$RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['tool_count'])")
FAILED=$(echo "$RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['failed_plugins'])")
CRON_COUNT=$(echo "$RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['cron_jobs'])")
HAS_SCHED=$(echo "$RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['has_publish_scheduler'])")
HAS_HEALTH=$(echo "$RESULT" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)['has_ops_health'])")

[ "$TOOL_COUNT" -ge "$MIN_TOOLS" ] || fail "tool.list=$TOOL_COUNT < $MIN_TOOLS (plugin 加载失败)"
[ "$FAILED" = "{}" ] || fail "failed_plugins 非空: $FAILED"
[ "$CRON_COUNT" -ge "$MIN_CRON_JOBS" ] || fail "cron.list=$CRON_COUNT < $MIN_CRON_JOBS (cron job 丢失)"
[ "$HAS_SCHED" = "True" ] || fail "publish_scheduler 缺失 (venv 依赖未对齐)"
[ "$HAS_HEALTH" = "True" ] || fail "ops_health 缺失"

log "✅ 部署成功: tools=$TOOL_COUNT failed_plugins={} cron_jobs=$CRON_COUNT publish_scheduler=ok ops_health=ok"
