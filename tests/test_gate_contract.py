import asyncio
import json
import os

import pytest

from douyin_tools.douyin_publish import DouyinPublishTool
from douyin_tools.queue_ops import QueueOpsTool
from douyin_tools.publish_scheduler import PublishSchedulerTool
from douyin_tools.self_repair import SelfRepairTool

GRAPHS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "graphs")

# gate 条件字面量 <-> 工具输出键 配对表.
# 审计 R4/H3: gate 右侧字面量靠巧合命中工具输出, 任一方改字面量即断链.
# 此测试锁死契约: 工具必须输出对应字面量, graph gate 必须引用对应键+字面量.
# ConditionEngine _resolve_literal 对裸词 true/false 不归一化为 bool, 走 str 保留 (审计 H3),
# 故工具必须输出小写字符串 "true"/"false" 而非 bool True/False 或大写.
GATE_CONTRACTS = [
    # (graph_file, gate_node, condition_expr, tool, expected_output_key, expected_literal)
    ("douyin_auto_publish.json", "gate_peak", "in_peak == true", "publish_scheduler", "in_peak", "true"),
    ("douyin_auto_publish.json", "gate_stock", "has_stock == true", "queue_ops", "has_stock", "true"),
    ("douyin_auto_publish.json", "gate_refilled", "enqueued_status == enqueued", "queue_ops", "status", "enqueued"),
    ("douyin_auto_publish.json", "gate_ok", "publish_status == publish_ok", "douyin_publish", "status", "publish_ok"),
    ("douyin_queue_publish.json", "gate_ok", "publish_status == publish_ok", "douyin_publish", "status", "publish_ok"),
    ("douyin_repair_publish.json", "gate_candidate", "has_candidate == true", "self_repair", "has_candidate", "true"),
]


def _load_graph(name):
    with open(os.path.join(GRAPHS_DIR, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("graph_file,gate,expr,tool,key,literal", GATE_CONTRACTS)
def test_gate_condition_matches_graph(graph_file, gate, expr, tool, key, literal):
    # 契约: graph 内 gate 的 condition_expr 必须引用工具实际输出的键, 右侧字面量必须等于工具输出值.
    g = _load_graph(graph_file)
    node = g["nodes"][gate]
    assert node["condition_expr"] == expr, f"{graph_file} {gate} 条件被改: {node['condition_expr']} != {expr}"
    # expr 形如 "var == literal", 拆出左侧变量名 + 右侧字面量.
    parts = expr.split("==")
    assert len(parts) == 2, f"{gate} 条件非 == 形式: {expr}"
    left_var = parts[0].strip()
    right_lit = parts[1].strip()
    assert right_lit == literal, f"{gate} 右侧字面量漂移: {right_lit} != {literal}"
    # 上游工具节点必须有 output_mapping 把工具输出键映射到 gate 左侧变量.
    # 找到 gate 前一个 tool 节点 (edges 中 target_id==gate 的 source_id).
    src_id = None
    for e in g["edges"]:
        if e.get("target_id") == gate:
            src_id = e.get("source_id")
            break
    assert src_id, f"{gate} 无前驱 edge"
    src_node = g["nodes"][src_id]
    assert src_node["type"] == "tool", f"{gate} 前驱 {src_id} 非 tool 节点"
    assert src_node["tool_name"] == tool, f"{gate} 前驱工具 {src_node['tool_name']} != 期望 {tool}"
    mapping = src_node["tool_params"].get("output_mapping", {})
    mapped_key = mapping.get(key)
    assert mapped_key == left_var, (
        f"{graph_file} {src_id} output_mapping 未把 {key} 映射到 gate 左侧 {left_var}: got {mapped_key}"
    )


def test_tool_outputs_string_boolean_not_bool():
    # 审计 H3: ConditionEngine 不归一化 bool. 工具输出布尔语义必须是小写字符串 "true"/"false",
    # 不能是 bool True/False (json.dumps 后 "true" 巧合命中, 但若工具改回 bool 类型变量插值会崩) 或大写.
    # publish_scheduler 输出 in_peak/should_wait.
    sched = PublishSchedulerTool()
    out = json.loads(asyncio.run(sched.execute()))
    assert out["in_peak"] in ("true", "false"), f"in_peak 非字符串布尔: {out['in_peak']!r}"
    assert out["should_wait"] in ("true", "false"), f"should_wait 非字符串布尔: {out['should_wait']!r}"
    # self_repair scan 输出 has_candidate.
    sr = SelfRepairTool()
    out = json.loads(asyncio.run(sr.execute(action="scan")))
    assert out["has_candidate"] in ("true", "false"), f"has_candidate 非字符串布尔: {out['has_candidate']!r}"


def test_queue_ops_dequeue_success_has_stock_string_true(tmp_path, monkeypatch):
    # 审计 H3: dequeue 成功 has_stock 必须字符串 "true" 匹配 gate_stock == true.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    asyncio.run(t.execute(action="enqueue", video_path=str(video),
                          script_json=json.dumps({"title": "x", "narration": "n", "scenes": []}), topic="x"))
    d = json.loads(asyncio.run(t.execute(action="dequeue")))
    assert d["has_stock"] == "true", f"dequeue has_stock 非 'true': {d['has_stock']!r}"


def test_queue_ops_enqueue_status_enqueued_literal(tmp_path, monkeypatch):
    # 审计 R4: enqueue 成功 status 必须字面量 "enqueued" 匹配 gate_refilled == enqueued.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    r = json.loads(asyncio.run(t.execute(action="enqueue", video_path=str(video),
                                          script_json=json.dumps({"title": "x", "narration": "n", "scenes": []}), topic="x")))
    assert r["status"] == "enqueued", f"enqueue status 非 'enqueued': {r['status']!r}"


def test_tool_error_returns_top_level_error_key():
    # 审计 R3: 工具失败必须返回顶层 {"error": ...} (runtime stop_on_tool_error 检测此 key 停 cascade).
    # 不能嵌套 error 或用别的 key, 否则 runtime 检测不到 -> gate 静默误路由.
    # 抽样验证关键工具失败路径: queue_ops enqueue 缺 video, douyin_publish 缺 video.
    q = QueueOpsTool()
    r = json.loads(asyncio.run(q.execute(action="enqueue", video_path="/nonexistent/none.mp4",
                                          script_json="{}", topic="")))
    assert "error" in r, f"queue_ops 失败无顶层 error key: {r}"
    assert isinstance(r["error"], str)
    p = DouyinPublishTool()
    r = json.loads(asyncio.run(p.execute(video_path="/nonexistent/none.mp4", description="x", dry_run=True)))
    assert "error" in r, f"douyin_publish 失败无顶层 error key: {r}"


def test_all_gate_graphs_have_stop_on_tool_error():
    # 审计 R3: gate 路由的 graph 必须开 stop_on_tool_error:true, 否则 {"error":...} 不停 cascade -> 误路由.
    # 含 condition 节点的生产 graph 全部校验.
    gate_graphs = [
        "douyin_auto_publish.json",
        "douyin_queue_publish.json",
        "douyin_repair_publish.json",
        "douyin_comment_reply.json",
        "douyin_batch_produce.json",
        "douyin_evolve.json",
    ]
    for gf in gate_graphs:
        g = _load_graph(gf)
        assert g.get("stop_on_tool_error") is True, f"{gf} 未开 stop_on_tool_error:true -> R3 gate 误路由风险"
