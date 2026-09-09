import json
import os

import pytest

from restock_chain import RESTOCK_CHAIN_STEPS, build_restock_chain_params

GRAPHS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "graphs")


class TestChainSpec:
    def test_chain_steps_count(self):
        assert len(RESTOCK_CHAIN_STEPS) == 5

    def test_step_titles(self):
        titles = [s["title"] for s in RESTOCK_CHAIN_STEPS]
        assert titles == ["补货-脚本", "补货-配图", "补货-配音", "补货-合成", "补货-入库"]

    def test_depends_on_indices_in_range(self):
        n = len(RESTOCK_CHAIN_STEPS)
        for idx, step in enumerate(RESTOCK_CHAIN_STEPS):
            for dep in step["depends_on"]:
                assert 0 <= dep < n, f"step {idx} dep {dep} out of range"
                assert dep != idx, f"step {idx} self-dependency"

    def test_no_cycle(self):
        visited = set()
        for idx, step in enumerate(RESTOCK_CHAIN_STEPS):
            for dep in step["depends_on"]:
                assert dep in visited, f"step {idx} dep {dep} not yet defined (cycle/order)"
            visited.add(idx)

    def test_script_no_deps(self):
        assert RESTOCK_CHAIN_STEPS[0]["depends_on"] == []

    def test_image_depends_on_script(self):
        assert RESTOCK_CHAIN_STEPS[1]["depends_on"] == [0]

    def test_tts_depends_only_on_script(self):
        assert RESTOCK_CHAIN_STEPS[2]["depends_on"] == [0]
        assert 1 not in RESTOCK_CHAIN_STEPS[2]["depends_on"]

    def test_compose_depends_on_image_and_tts(self):
        assert RESTOCK_CHAIN_STEPS[3]["depends_on"] == [1, 2]

    def test_enqueue_depends_on_compose(self):
        assert RESTOCK_CHAIN_STEPS[4]["depends_on"] == [3]

    def test_every_step_has_graph_id(self):
        for idx, step in enumerate(RESTOCK_CHAIN_STEPS):
            assert step["graph_id"], f"step {idx} missing graph_id"

    def test_every_step_has_role(self):
        valid_roles = {"lead", "researcher", "writer", "imager", "voicer", "publisher", "analyst"}
        for idx, step in enumerate(RESTOCK_CHAIN_STEPS):
            assert step["role"] in valid_roles, f"step {idx} invalid role {step['role']}"

    def test_every_step_has_output_keys(self):
        for idx, step in enumerate(RESTOCK_CHAIN_STEPS):
            assert step["output_keys"], f"step {idx} missing output_keys"

    def test_every_step_has_max_retries(self):
        for idx, step in enumerate(RESTOCK_CHAIN_STEPS):
            assert step["max_retries"] >= 1, f"step {idx} max_retries < 1"

    def test_roles_match_topology(self):
        assert RESTOCK_CHAIN_STEPS[0]["role"] == "writer"
        assert RESTOCK_CHAIN_STEPS[1]["role"] == "imager"
        assert RESTOCK_CHAIN_STEPS[2]["role"] == "voicer"
        assert RESTOCK_CHAIN_STEPS[3]["role"] == "publisher"
        assert RESTOCK_CHAIN_STEPS[4]["role"] == "publisher"


class TestBuildParams:
    def test_build_restock_chain_params_step0_input(self):
        params = build_restock_chain_params("topic_a", "series_b", "hook_c")
        steps = params["steps"]
        assert steps[0]["input"] == {"topic": "topic_a", "series": "series_b", "hook_variant": "hook_c"}

    def test_build_restock_chain_params_other_steps_empty_input(self):
        params = build_restock_chain_params("t", "s", "h")
        for idx in range(1, 5):
            assert params["steps"][idx]["input"] == {}, f"step {idx} input not empty"

    def test_build_restock_chain_params_team(self):
        params = build_restock_chain_params("t", "s", "h", team="video_a")
        assert params["team"] == "video_a"

    def test_build_restock_chain_params_default_team(self):
        params = build_restock_chain_params("t", "s", "h")
        assert params["team"] == "default"

    def test_build_restock_chain_params_preserves_step_fields(self):
        params = build_restock_chain_params("t", "s", "h")
        for idx, step in enumerate(params["steps"]):
            assert step["graph_id"] == RESTOCK_CHAIN_STEPS[idx]["graph_id"]
            assert step["depends_on"] == RESTOCK_CHAIN_STEPS[idx]["depends_on"]
            assert step["role"] == RESTOCK_CHAIN_STEPS[idx]["role"]
            assert step["output_keys"] == RESTOCK_CHAIN_STEPS[idx]["output_keys"]


class TestMiniGraphs:
    @pytest.mark.parametrize("graph_file", [
        "douyin_step_script.json",
        "douyin_step_image.json",
        "douyin_step_tts.json",
        "douyin_step_compose.json",
        "douyin_step_enqueue.json",
    ])
    def test_mini_graph_exists_and_valid(self, graph_file):
        path = os.path.join(GRAPHS_DIR, graph_file)
        assert os.path.exists(path), f"missing {graph_file}"
        with open(path) as f:
            g = json.load(f)
        assert g["start_node_id"] == "start"
        assert "nodes" in g and isinstance(g["nodes"], dict)
        assert "edges" in g and isinstance(g["edges"], list)
        assert "start" in g["nodes"] and g["nodes"]["start"]["type"] == "start"
        assert "end" in g["nodes"] and g["nodes"]["end"]["type"] == "end"
        tool_nodes = [n for n in g["nodes"].values() if n["type"] == "tool"]
        assert len(tool_nodes) == 1, f"{graph_file} should have exactly 1 tool node"
        assert tool_nodes[0].get("tool_name"), f"{graph_file} tool node missing tool_name"
        assert tool_nodes[0]["tool_params"].get("output_mapping"), f"{graph_file} missing output_mapping"

    def test_mini_graph_step_mapping(self):
        for step in RESTOCK_CHAIN_STEPS:
            path = os.path.join(GRAPHS_DIR, step["graph_id"] + ".json")
            assert os.path.exists(path), f"step graph {step['graph_id']} not found"
