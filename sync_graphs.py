import asyncio
import glob
import json
import logging
import os
import sys

from agent_runtime.sdk.client import AgentClient

from douyin_tools.config import AGENT_STUDIO_SOCKET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sync_graphs")

OPERATION_DIR = os.path.dirname(os.path.abspath(__file__))
GRAPHS_DIR = os.path.join(OPERATION_DIR, "graphs")

# 生产 graph 白名单 (审计 R2): 只同步这些进 daemon store, 隔离本地验证用 graph.
# douyin_daily_publish.json 是验证用僵尸 graph (旧 mlx_image/mlx_tts 路径 + description:"{{script_json}}" bug),
# sync 进 store 会被 cron 误调度或 GUI 误点 -> 旧路径造片失败. 白名单挡住, 文件保留供本地 CLI 验证.
PRODUCTION_GRAPHS = [
    "douyin_auto_publish.json",
    "douyin_batch_produce.json",
    "douyin_queue_publish.json",
    "douyin_comment_reply.json",
    "douyin_comment_reply_winner.json",
    "douyin_evolve.json",
    "douyin_repair_publish.json",
]

# graph name -> 关联 cron job id 列表. sync 重建 graph 后 graph_id 变化,
# 须回填 cron job 的 graph_id 否则 cron 触发 Graph not found (8-20 11:05 根因).
CRON_BY_GRAPH = {
    "douyin_auto_publish": [
        "douyin_publish_plan_0905",
        "douyin_publish_plan_1105",
        "douyin_publish_plan_1405",
        "douyin_publish_plan_1805",
    ],
    "douyin_comment_reply": [
        "douyin_comment_plan_1335",
        "douyin_comment_plan_2135",
    ],
    "douyin_evolve": ["douyin_evolve_plan_2315"],
    "douyin_repair_publish": ["douyin_repair_plan_2345"],
}

# cron job id -> 完整字段 (expression/input_data). 回填时用 cron.register
# 覆盖更新 (INSERT OR REPLACE), 必须传完整字段否则 expression 默认 * * * * * 危险.
CRON_FIELDS = {
    "douyin_publish_plan_0905": ("5 9 * * *", '{"dry_run":"false"}'),
    "douyin_publish_plan_1105": ("5 11 * * *", '{"dry_run":"false"}'),
    "douyin_publish_plan_1405": ("5 14 * * *", '{"dry_run":"false"}'),
    "douyin_publish_plan_1805": ("5 18 * * *", '{"dry_run":"false"}'),
    "douyin_comment_plan_1335": ("35 13 * * *", ""),
    "douyin_comment_plan_2135": ("35 21 * * *", ""),
    "douyin_evolve_plan_2315": ("15 23 * * *", ""),
    "douyin_repair_plan_2345": ("45 23 * * *", ""),
}


async def _list_graphs(client):
    res = await client.call("graph.list", {})
    if res.get("error"):
        logger.error("graph.list failed: %s", res["error"])
        return []
    return res.get("graphs", [])


async def _delete_graph(client, graph_id):
    res = await client.call("graph.delete", {"graph_id": graph_id})
    if res.get("error"):
        logger.warning("graph.delete %s failed: %s", graph_id, res["error"])
        return False
    return res.get("deleted", False)


async def _create_graph(client, graph_data):
    res = await client.call("graph.create", {"graph_data": graph_data})
    if res.get("error"):
        logger.error("graph.create failed: %s", res["error"])
        return ""
    return res.get("graph_id", "")


async def _backfill_cron_graph_id(client, graph_name, new_id):
    # sync 重建 graph 后回填关联 cron job 的 graph_id.
    # 用 cron.register 覆盖更新 (INSERT OR REPLACE), 必须传完整字段.
    job_ids = CRON_BY_GRAPH.get(graph_name, [])
    if not job_ids:
        return 0
    # B14 (审计 B14): 旧硬编码 enabled=True 覆盖运营者手动禁用的 job.
    # sync 部署不应改变 enabled 状态. 先 cron.list 读现有 enabled, 缺失回退 True.
    existing_enabled = {}
    try:
        cl = await client.call("cron.list", {})
        for j in cl.get("jobs", []):
            existing_enabled[j.get("id", "")] = j.get("enabled", True)
    except Exception as e:
        logger.warning("cron.list failed for enabled preserve, fallback True: %s", e)
    backfilled = 0
    for jid in job_ids:
        expr, input_data = CRON_FIELDS.get(jid, ("", ""))
        if not expr:
            logger.warning("cron backfill skip unknown job %s", jid)
            continue
        preserved_enabled = existing_enabled.get(jid, True)
        res = await client.call("cron.register", {
            "id": jid,
            "name": jid,
            "expression": expr,
            "graph_id": new_id,
            "input_data": input_data,
            "enabled": preserved_enabled,
            "max_retries": 1,
        })
        if res.get("status") == "ok":
            backfilled += 1
            logger.info("cron backfill ok job=%s graph_id=%s", jid, new_id)
        else:
            logger.error("cron backfill fail job=%s res=%s", jid, res)
    return backfilled


async def sync_graphs(graph_files):
    client = AgentClient(socket_path=AGENT_STUDIO_SOCKET)
    existing = await _list_graphs(client)
    logger.info("store 现有 %d 个 graph", len(existing))

    by_name = {}
    for g in existing:
        name = g.get("name", "")
        by_name.setdefault(name, []).append(g.get("graph_id", g.get("id", "")))

    synced = 0
    for path in graph_files:
        with open(path, encoding="utf-8") as f:
            graph_data = json.load(f)
        name = graph_data.get("name", os.path.basename(path))
        stale_ids = by_name.get(name, [])
        for gid in stale_ids:
            ok = await _delete_graph(client, gid)
            if ok:
                logger.info("删除旧 graph name=%s id=%s", name, gid)
            else:
                # 删失败不 abort 整体 sync, 记录残留待下次清理.
                # graph_id 漂移根因由 _backfill_cron_graph_id 兜底: 新 id 回填 cron.
                logger.warning("删旧 graph 失败 name=%s id=%s 残留待下次清理", name, gid)
        new_id = await _create_graph(client, graph_data)
        if new_id:
            synced += 1
            logger.info(
                "sync ok name=%s graph_id=%s agent_id=%s nodes=%d",
                name,
                new_id,
                graph_data.get("agent_id", ""),
                len(graph_data.get("nodes", {})),
            )
            # 回填关联 cron job 的 graph_id (修 graph_id 漂移: cron 绑死旧 id 失效).
            n = await _backfill_cron_graph_id(client, name, new_id)
            if n:
                logger.info("cron 回填 %d 个 job -> graph_id=%s", n, new_id)
        else:
            logger.error("sync 失败 name=%s path=%s", name, path)
    return synced


def main():
    # 只同步生产白名单 graph (审计 R2): 隔离本地验证用/废弃 graph 不进 daemon store.
    # 旧实现 glob(*.json) 全同步, 把 douyin_daily_publish (旧路径+script_json bug) 等非生产 graph
    # 也推进 store -> cron 误调度或 GUI 误点 -> 旧路径造片失败.
    all_files = sorted(glob.glob(os.path.join(GRAPHS_DIR, "*.json")))
    graph_files = [
        os.path.join(GRAPHS_DIR, name) for name in PRODUCTION_GRAPHS
        if os.path.join(GRAPHS_DIR, name) in all_files
    ]
    skipped = [f for f in all_files if f not in graph_files]
    if skipped:
        logger.warning("跳过非生产 graph (白名单外): %s", [os.path.basename(f) for f in skipped])
    if not graph_files:
        logger.error("graphs 目录无生产 graph: %s", GRAPHS_DIR)
        return 1
    logger.info("生产白名单 %d 个 graph (目录共 %d)", len(graph_files), len(all_files))
    synced = asyncio.run(sync_graphs(graph_files))
    logger.info("完成：sync %d/%d 个 graph 进 daemon store", synced, len(graph_files))
    return 0 if synced == len(graph_files) else 1


if __name__ == "__main__":
    sys.exit(main())
