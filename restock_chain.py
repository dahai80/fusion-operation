"""M3-2: 补货链拆解为看板任务依赖.

5 步 DAG (script→image→tts→compose→enqueue), 每步单工具 mini-graph.
单环节失败只打回该环节 (orchestrator step retry), 不重跑上游.

DAG 结构 (非线性, 按真实数据依赖):
    script(0) → image(1) ─┐
    script(0) → tts(2) ───┤→ compose(3) → enqueue(4)

tts 只依赖 script (需要 script_json), 不依赖 image — 避免配音等配图.
compose 依赖 image+tts (需要 image_paths + audio_path).
orchestrator 按拓扑序执行: 0→1→2→3→4 (顺序模型, DAG 仅声明依赖).
"""

import logging

logger = logging.getLogger(__name__)

RESTOCK_CHAIN_STEPS = [
    {
        "title": "补货-脚本",
        "graph_id": "douyin_step_script",
        "depends_on": [],
        "role": "writer",
        "output_keys": ["script_json"],
        "max_retries": 2,
    },
    {
        "title": "补货-配图",
        "graph_id": "douyin_step_image",
        "depends_on": [0],
        "role": "imager",
        "output_keys": ["image_paths"],
        "max_retries": 2,
    },
    {
        "title": "补货-配音",
        "graph_id": "douyin_step_tts",
        "depends_on": [0],
        "role": "voicer",
        "output_keys": ["audio_path"],
        "max_retries": 2,
    },
    {
        "title": "补货-合成",
        "graph_id": "douyin_step_compose",
        "depends_on": [1, 2],
        "role": "publisher",
        "output_keys": ["video_path"],
        "max_retries": 1,
    },
    {
        "title": "补货-入库",
        "graph_id": "douyin_step_enqueue",
        "depends_on": [3],
        "role": "publisher",
        "output_keys": ["enqueued_status"],
        "max_retries": 1,
    },
]


def build_restock_chain_params(topic, series, hook_variant, team="default"):
    # 组装 task.run_chain RPC params.
    # topic_gen 仍由 auto_publish graph 跑 (选题不拆), 产物经 chain step 0 input 传入.
    steps = []
    for step in RESTOCK_CHAIN_STEPS:
        s = dict(step)
        s["input"] = {}
        steps.append(s)
    steps[0]["input"] = {"topic": topic, "series": series, "hook_variant": hook_variant}
    logger.info("restock chain built: %d steps team=%s topic=%s", len(steps), team, topic)
    return {"steps": steps, "team": team}
