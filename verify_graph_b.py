import asyncio
import json
import logging
import os
import sys

from agent_runtime.graph import AgentGraph
from agent_runtime.runtime import AgentRuntime
from agent_runtime.context import AgentEventType

from douyin_tools import create_douyin_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("verify_graph_b")

TEST_COMMENTS = [
    {"id": "cmt_001", "text": "太震撼了，太阳系居然还有第九大行星"},
    {"id": "cmt_002", "text": "为什么科学家到现在才发现？"},
    {"id": "cmt_003", "text": "下期讲讲黑洞吧"},
    {"id": "cmt_004", "text": "关注了，讲得很好"},
    {"id": "cmt_005", "text": "这个角度的科普很新颖"},
]

GATE_GRAPH = {
    "name": "verify_gate",
    "description": "验证 has_new 条件路由 true->draft / false->end",
    "start_node_id": "start",
    "version": "1.0",
    "nodes": {
        "start": {"type": "start", "label": "开始"},
        "gate": {"type": "condition", "label": "是否有新评论", "condition_expr": "has_new == true"},
        "draft": {"type": "tool", "tool_name": "mlx_reply_draft", "label": "草拟回复",
                  "tool_params": {"comments_json": "{{comments_json}}", "account_role": "科普博主",
                                  "output_mapping": {"drafts": "drafts_json"}}},
        "end": {"type": "end", "label": "结束"},
    },
    "edges": [
        {"source_id": "start", "target_id": "gate"},
        {"source_id": "gate", "target_id": "draft", "label": "true"},
        {"source_id": "gate", "target_id": "end", "label": "false"},
        {"source_id": "draft", "target_id": "end"},
    ],
}


async def collect(runtime, graph, label):
    last_result = ""
    visited = []
    completed = False
    async for event in runtime.execute_graph(graph):
        logger.info("[%s] event=%s node=%s name=%s", label, event.type.value, event.node_id, event.name)
        visited.append(event.node_id or "")
        if event.type == AgentEventType.ERROR:
            logger.error("[%s] graph error: %s", label, event.content)
            return None, visited, event.content
        if event.type == AgentEventType.TOOL_RESULT:
            last_result = event.content
        if event.type == AgentEventType.END:
            completed = True
    return last_result, visited, "completed" if completed else "not_completed"


async def main():
    os.environ.pop("FUSION_CLIENT_KEY", None)  # issue #23: 只清 canonical 名, 别名链归 fusion-core
    registry = create_douyin_registry()
    logger.info("registry tools=%d", registry.count)

    # --- Sub-path 1: draft(真实模型) -> reply(dry_run) ---
    draft_reply_graph = {
        "name": "verify_draft_reply",
        "description": "验证真实模型草拟 + reply dry_run 计划",
        "start_node_id": "start",
        "version": "1.0",
        "nodes": {
            "start": {"type": "start", "label": "开始"},
            "draft": {"type": "tool", "tool_name": "mlx_reply_draft", "label": "草拟回复",
                      "tool_params": {"comments_json": "{{comments_json}}", "account_role": "科普博主",
                                      "output_mapping": {"drafts": "drafts_json"}}},
            "reply": {"type": "tool", "tool_name": "douyin_reply", "label": "点赞+回复(dry_run)",
                      "tool_params": {"drafts_json": "{{drafts_json}}", "max_reply": 5, "dry_run": True}},
            "end": {"type": "end", "label": "结束"},
        },
        "edges": [
            {"source_id": "start", "target_id": "draft"},
            {"source_id": "draft", "target_id": "reply"},
            {"source_id": "reply", "target_id": "end"},
        ],
    }
    g1 = AgentGraph.from_dict(draft_reply_graph)
    errs = g1.validate()
    if errs:
        logger.error("draft_reply_graph invalid: %s", errs)
        return 1
    rt1 = AgentRuntime(tool_registry=registry)
    rt1.variables.set("comments_json", json.dumps(TEST_COMMENTS, ensure_ascii=False))
    logger.info("=== sub-path1 draft(真实模型)->reply(dry_run) ===")
    last1, visited1, status1 = await collect(rt1, g1, "draft_reply")
    drafts_var = rt1.variables.to_dict().get("drafts_json")
    logger.info("drafts_var raw: %s", str(drafts_var)[:800])
    drafts = drafts_var
    if isinstance(drafts, str):
        try:
            drafts = json.loads(drafts)
        except (ValueError, TypeError):
            import ast
            try:
                drafts = ast.literal_eval(drafts)
            except (ValueError, SyntaxError):
                drafts = None
    if not drafts:
        logger.error("FAIL: drafts 为空")
        return 1
    if not drafts or not isinstance(drafts, list) or len(drafts) == 0:
        logger.error("FAIL: drafts 为空或非列表")
        return 1
    safe_count = sum(1 for d in drafts if d.get("safe") is True)
    logger.info("drafts count=%d safe=%d", len(drafts), safe_count)
    if safe_count == 0:
        logger.error("FAIL: 无任何 safe=True 的草稿")
        return 1
    logger.info("reply dry_run result: %s", last1[:400])
    try:
        plan = json.loads(last1)
        assert plan.get("dry_run") is True
        assert isinstance(plan.get("todo"), list)
        logger.info("reply todo=%s skipped=%d", plan.get("todo"), plan.get("skipped"))
    except (ValueError, TypeError, AssertionError) as e:
        logger.error("FAIL: reply dry_run 计划校验失败: %s | %s", e, last1[:200])
        return 1
    logger.info("sub-path1 OK visited=%s status=%s", visited1, status1)

    # --- Sub-path 2: gate has_new=false -> end (draft 不应执行) ---
    g2 = AgentGraph.from_dict(GATE_GRAPH)
    errs2 = g2.validate()
    if errs2:
        logger.error("gate_graph invalid: %s", errs2)
        return 1
    rt2 = AgentRuntime(tool_registry=registry)
    rt2.variables.set("has_new", "false")
    logger.info("=== sub-path2 gate has_new=false->end ===")
    last2, visited2, status2 = await collect(rt2, g2, "gate_false")
    if "草拟回复" in visited2:
        logger.error("FAIL: has_new=false 时 draft 被执行 visited=%s", visited2)
        return 1
    if status2 != "completed":
        logger.error("FAIL: gate_false 未完成 status=%s", status2)
        return 1
    logger.info("sub-path2 OK visited=%s (draft 未执行)", visited2)

    # --- Sub-path 3: gate has_new=true -> draft（路由正确性） ---
    rt3 = AgentRuntime(tool_registry=registry)
    rt3.variables.set("has_new", "true")
    rt3.variables.set("comments_json", json.dumps(TEST_COMMENTS[:1], ensure_ascii=False))
    logger.info("=== sub-path3 gate has_new=true->draft ===")
    last3, visited3, status3 = await collect(rt3, g2, "gate_true")
    if "草拟回复" not in visited3:
        logger.error("FAIL: has_new=true 时 draft 未被执行 visited=%s", visited3)
        return 1
    logger.info("sub-path3 OK visited=%s", visited3)

    logger.info("=== Graph B 验证全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
