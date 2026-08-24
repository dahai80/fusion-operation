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

_graph_id_cache = {}

# RPC 超时: daemon 接收请求但无响应 (事件循环卡死/graph 长任务) 时上游 AgentClient.call
# 的 reader.readline() 永久 await. 上游 PR #208 (issue #207) 已加 native timeout 参数,
# 构造时传 default_timeout 即获超时保护, 超时返回 error dict 让 cron job 落 failed 而非
# 永久挂起叠加下一次 fire. 旧 CLI 层 wait_for 包装已移除 (上游 SDK 层接管).
RPC_TIMEOUT = float(os.environ.get("FUSION_RPC_TIMEOUT", "600"))


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
    graph_id = _graph_id_cache.get(cache_key)
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
        _graph_id_cache[cache_key] = graph_id
        logger.info("rpc graph.create ok graph_id=%s name=%s", graph_id, res.get("name", ""))
    else:
        logger.info("rpc reuse graph_id=%s", graph_id)
    logger.info("rpc graph.execute graph_id=%s approve=%s vars=%s", graph_id, approve, list(variables.keys()))
    exec_params = {"graph_id": graph_id, "variables": variables}
    if agent_id:
        exec_params["agent_id"] = agent_id
    res = await client.call("graph.execute", exec_params)
    if res.get("error"):
        logger.warning("rpc graph.execute failed: %s", res["error"])
        return None
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
    rpc_res = await _rpc_run_once(graph_path, approve, extra_vars)
    if rpc_res is not None:
        return rpc_res
    logger.warning("rpc unavailable, fallback to local import path")
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
    variants = ["A", "B", "C"]
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
        if rc != 0:
            logger.error("publish round %d failed", i + 1)
            break
        try:
            d = json.loads(res)
        except (ValueError, TypeError):
            d = {}
        if d.get("status") == "empty":
            logger.info("queue empty, stop publishing")
            break
        if d.get("status") == "publish_failed":
            logger.error("publish failed, stop (item kept in pending for retry)")
            break
        if d.get("in_peak") == "false":
            logger.info("off-peak now=%s next_window=%s, stop (wait for peak)", d.get("now"), d.get("next_window"))
            break
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
