import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys

from agent_runtime.sdk.client import AgentClient

from douyin_tools.config import AGENT_STUDIO_SOCKET, get_json_config, mlx_health, reload_json_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("run_graph")

import time as _time

_graph_id_cache = {}
# graph_id 缓存 TTL (审计 P1-4): 永久缓存 + sync_graphs 重建/daemon 重启改 id -> stale ->
# "Graph not found" 被当 RPC 不可用 -> 静默本地回退 (无 tool config 注入, 与 cron 分叉).
# 60s TTL + execute 失败时清缓存重取一次.
_GRAPH_ID_TTL = 60.0

# RPC 超时: daemon 接收请求但无响应 (事件循环卡死/graph 长任务) 时上游 AgentClient.call
# 的 reader.readline() 永久 await. 上游 PR #208 (issue #207) 已加 native timeout 参数,
# 构造时传 default_timeout 即获超时保护, 超时返回 error dict 让 cron job 落 failed 而非
# 永久挂起叠加下一次 fire. 旧 CLI 层 wait_for 包装已移除 (上游 SDK 层接管).
# 审计 0908 F2: 旧默认 600s < 补货链真实执行时长 (topic_gen≤600 + mlx_script≤600 +
# comfyui_image≤900 + comfyui_tts≤300 + compose + publish 5-15min, 尾部 60-90min).
# 600s 必超时 -> client 记 failed 但 daemon 仍后台执行真发布 (孤儿执行) -> lease 到期
# _reclaim_expired 移回 pending -> 下轮重发同一条 (重复发布). 上调到 5400s (90min) 覆盖
# 最坏情形: client 等 daemon 跑完返回真实结果, 无孤儿无假失败无重发. cron 最小间隔 2h,
# 90min < 120min 不致下轮 fire 重叠. 真死锁 (事件循环卡死) 由 ops_health inflight>2×TTL
# 告警捕获 (修 ④). 完整异步化 (execution_id + 事件订阅) 属 M1 团队状态层, 此处止血上调.
RPC_TIMEOUT = float(os.environ.get("FUSION_RPC_TIMEOUT", "5400"))


class _ExecAttempted(Exception):
    # graph.execute 已在 daemon 端执行 (可能部分) -> 不可回退本地重跑 (审计 P2-4).
    pass


async def _rpc_run_once(graph_path, approve, extra_vars):
    client = AgentClient(socket_path=AGENT_STUDIO_SOCKET, default_timeout=RPC_TIMEOUT)
    variables = {
        "safety_approved": "true" if approve else "false",
        "dry_run": "false" if approve else "true",
    }
    if extra_vars:
        variables.update({k: str(v) for k, v in extra_vars.items()})
    agent_id = (get_json_config().get("agent_studio") or {}).get("agent_id", "")
    if agent_id:
        logger.info("rpc inject agent_id=%s for tool config injection", agent_id)
    # E2: cache_key 用内容 hash 不用 mtime. mtime 随 touch/原地重写变化 (sync_graphs/
    # 编辑器保存都改 mtime 即使内容没变) -> 误失效缓存 -> 重复 graph.create RPC 浪费.
    # 内容 hash 同内容复用 graph_id, 内容真改才重建.
    with open(graph_path, "rb") as _f:
        content_hash = hashlib.md5(_f.read()).hexdigest()
    cache_key = f"{graph_path}:{content_hash}"
    cached = _graph_id_cache.get(cache_key)
    graph_id = ""
    if cached and (_time.monotonic() - cached[1] < _GRAPH_ID_TTL):
        graph_id = cached[0]
        logger.info("rpc reuse graph_id=%s", graph_id)
    if not graph_id:
        with open(graph_path, encoding="utf-8") as f:
            graph_data = json.load(f)
        res = await client.call("graph.create", {"graph_data": graph_data})
        if res.get("error"):
            logger.warning("rpc graph.create failed: %s", res["error"])
            return None
        graph_id = res.get("graph_id", "")
        if not graph_id:
            logger.warning("rpc graph.create no graph_id")
            return None
        _graph_id_cache[cache_key] = (graph_id, _time.monotonic())
        logger.info("rpc graph.create ok graph_id=%s name=%s", graph_id, res.get("name", ""))
    logger.info("rpc graph.execute graph_id=%s approve=%s vars=%s", graph_id, approve, list(variables.keys()))
    exec_params = {"graph_id": graph_id, "variables": variables}
    if agent_id:
        exec_params["agent_id"] = agent_id
    res = await client.call("graph.execute", exec_params)
    if res.get("error"):
        logger.warning("rpc graph.execute failed: %s", res["error"])
        # execute 失败可能 stale graph_id -> 清缓存让下次重取 (审计 P1-4).
        _graph_id_cache.pop(cache_key, None)
        # daemon 已收到 execute 请求, 可能部分执行 -> 不回退本地 (审计 P2-4 双重执行).
        raise _ExecAttempted(res["error"])
    events = res.get("events", [])
    last_result = ""
    completed = res.get("status") == "completed"
    for ev in events:
        ev_type = ev.get("type", "")
        logger.info("event type=%s node=%s name=%s", ev_type, ev.get("node_id", ""), ev.get("name", ""))
        if ev_type == "error":
            logger.error("graph error: %s", ev.get("content", ""))
            return 1, ""
        if ev_type == "tool_result":
            last_result = ev.get("content", "")
    logger.info("rpc graph done completed=%s events=%d", completed, len(events))
    logger.info("last tool result: %s", last_result[:400])
    return (0 if completed else 1), last_result


async def _local_run_once(graph_path, approve=True, extra_vars=None):
    from agent_runtime.graph import AgentGraph
    from agent_runtime.runtime import AgentRuntime
    from agent_runtime.context import AgentEventType
    from douyin_tools import create_douyin_registry

    with open(graph_path, encoding="utf-8") as f:
        graph = AgentGraph.from_dict(json.load(f))
    errors = graph.validate()
    if errors:
        logger.error("graph invalid: %s", errors)
        return 1, ""
    registry = create_douyin_registry()
    runtime = AgentRuntime(tool_registry=registry)
    runtime.variables.set("safety_approved", "true" if approve else "false")
    runtime.variables.set("dry_run", "false" if approve else "true")
    if extra_vars:
        for k, v in extra_vars.items():
            runtime.variables.set(k, v)
            logger.info("inject var %s=%s", k, str(v)[:80])
    logger.info("run graph=%s approve=%s tools=%d (local fallback)", graph.name, approve, registry.count)
    last_result = ""
    completed = False
    async for event in runtime.execute_graph(graph):
        logger.info("event type=%s node=%s name=%s", event.type.value, event.node_id, event.name)
        if event.type == AgentEventType.ERROR:
            logger.error("graph error: %s", event.content)
            return 1, ""
        if event.type == AgentEventType.TOOL_RESULT:
            last_result = event.content
        if event.type == AgentEventType.END:
            completed = True
    logger.info("graph done completed=%s", completed)
    logger.info("final variables: %s", json.dumps(runtime.variables.to_dict(), ensure_ascii=False)[:600])
    logger.info("last tool result: %s", last_result[:400])
    return (0 if completed else 1), last_result


async def run_once(graph_path, approve=True, extra_vars=None):
    try:
        rpc_res = await _rpc_run_once(graph_path, approve, extra_vars)
    except _ExecAttempted as e:
        # daemon 已执行 graph.execute (可能部分) -> 不回退本地避免双重执行 (审计 P2-4).
        logger.error("rpc execute attempted but failed: %s -> 不回退本地, 返回失败", e)
        return 1, ""
    if rpc_res is not None:
        return rpc_res
    logger.warning("rpc unavailable (create 阶段未执行), fallback to local import path")
    return await _local_run_once(graph_path, approve, extra_vars)



async def run_batch_produce(graph_path, count, series="宇宙奥秘"):
    logger.info("batch produce start count=%d series=%s", count, series)
    from douyin_tools.topic_gen import TopicGenTool
    topic_tool = TopicGenTool()
    topics_res = await topic_tool.execute(series=series, count=count)
    topics_data = json.loads(topics_res)
    if topics_data.get("error") or not topics_data.get("topics"):
        logger.error("topic_gen failed: %s", topics_res[:300])
        return 1
    topics = topics_data["topics"]
    logger.info("topic_gen got %d topics", len(topics))
    ok = 0
    # variant C 4/4 全 0 播放 (2026-09-07 数据), 暂淘汰改 A/B 加权.
    # A max=1149 (破千) B max=995, 两变体均验证有效. C 待样本重新评估再开.
    variants = ["A", "B", "A", "B"]
    for i, t in enumerate(topics[:count]):
        topic = t.get("topic", "")
        hook_type = t.get("hook_type", "悬念")
        hook_variant = variants[i % len(variants)]
        logger.info(
            "=== batch round %d/%d topic=%s hook_variant=%s ===",
            i + 1, count, topic, hook_variant,
        )
        rc, _ = await run_once(
            graph_path,
            approve=True,
            extra_vars={
                "topic": topic,
                "series": series,
                "hook_type": hook_type,
                "hook_variant": hook_variant,
            },
        )
        if rc == 0:
            ok += 1
        else:
            logger.error("batch round %d failed topic=%s", i + 1, topic)
        await asyncio.sleep(2)
    logger.info("batch produce done ok=%d/%d", ok, count)
    return 0 if ok > 0 else 1


async def run_queue_publish(graph_path, daily_limit, approve=True):
    logger.info("queue publish start daily_limit=%d approve=%s", daily_limit, approve)
    published = 0
    for i in range(daily_limit):
        logger.info("=== publish round %d/%d ===", i + 1, daily_limit)
        rc, res = await run_once(graph_path, approve=approve)
        try:
            d = json.loads(res) if res else {}
        except (ValueError, TypeError):
            d = {}
        # 正常停止条件: 空库存/非高峰 (审计 P1-5): 旧实现 rc!=0 一律 break, 一次 flaky
        # 工具错误杀掉当天剩余全部发布. 空库存/非高峰本身 rc 可能非 0, 但属正常停止.
        if d.get("status") == "empty":
            logger.info("queue empty, stop publishing")
            break
        # B1 (审计 B1): publish 失败时 queue_ops 已把 item 从 inflight requeue 回 pending
        # (返回 {"status":"requeued"}), 最后 tool_result 是 requeued 非 publish_failed.
        # 旧判 publish_failed 永不命中 -> 失败 item 立即重试同条耗 daily_limit.
        # 改判 requeued: 失败已回 pending 等下轮 cron, 本轮停止不浪费当天配额.
        if d.get("status") == "requeued":
            logger.error("publish failed -> requeued, stop (item back in pending for next cron)")
            break
        if d.get("in_peak") == "false":
            logger.info("off-peak now=%s next_window=%s, stop (wait for peak)", d.get("now"), d.get("next_window"))
            break
        if rc != 0:
            # graph 异常 (非空库存/非高峰) -> continue 重试下一轮, 不杀全天 (审计 P1-5).
            logger.error("publish round %d graph error rc=%d, continue to next round", i + 1, rc)
            await asyncio.sleep(3)
            continue
        if d.get("status") == "archived":
            published += 1
        await asyncio.sleep(3)
    logger.info("queue publish done published=%d", published)
    return 0


def main():
    parser = argparse.ArgumentParser(description="抖音吸粉 DAG runner")
    parser.add_argument("graph", help="graph json 路径")
    parser.add_argument("approve", nargs="?", default="true", help="true=审批通过，false=不通过")
    parser.add_argument("--loop", type=int, default=0, help="批量造片条数(Graph C) 或 日发布上限(Graph D)，0=单次")
    parser.add_argument("--series", default="宇宙奥秘", help="选题系列(Graph C 用)")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VAL",
                        help="注入 graph 变量 (可重复, 如 --var aweme_title=最冷的地方)")
    args = parser.parse_args()
    # E5: --var key=val 解析成 extra_vars, 经 run_once -> graph.execute variables 注入.
    # winner graph aweme_title 等业务参数变量化, 改目标不改 graph JSON.
    cli_vars = {}
    for item in args.var:
        if "=" not in item:
            logger.warning("ignoring malformed --var (no =): %s", item)
            continue
        k, v = item.split("=", 1)
        cli_vars[k.strip()] = v.strip()
    approve = args.approve != "false"
    is_batch = "batch_produce" in args.graph
    # 每次运行重读 douyin_config.json (cron 每 fire 起新进程, 改配置立即生效无需重启 daemon, 修 A1).
    reload_json_config()
    is_queue = "queue_publish" in args.graph
    # 预检 mlx 鉴权/可达: cron 路径 LLM 调用前先诊断, 401/403 直接告警 (A5).
    h = mlx_health()
    if h["ok"]:
        logger.info("preflight mlx ok models=%d", h["model_count"])
    else:
        logger.error("preflight mlx FAIL http=%s reason=%s (graph 可能 LLM 空输出)", h["http"], h["reason"])
    if is_batch and args.loop > 0:
        rc = asyncio.run(run_batch_produce(args.graph, args.loop, args.series))
    elif is_queue and args.loop > 0:
        rc = asyncio.run(run_queue_publish(args.graph, args.loop, approve))
    else:
        rc, _ = asyncio.run(run_once(args.graph, approve, extra_vars=cli_vars or None))
    sys.exit(rc)


if __name__ == "__main__":
    main()
