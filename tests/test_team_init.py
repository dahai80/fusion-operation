import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.team_init import (
    _list_templates,
    _template_vars,
    init_team,
)


def test_list_templates_includes_douyin():
    templates = _list_templates()
    assert "douyin-ops" in templates


def test_template_vars_defaults():
    vars = _template_vars("douyin", "douyin-ops")
    assert vars["vertical"] == "douyin"
    assert vars["team"] == "douyin-ops"
    assert vars["template"] == "douyin-ops"
    assert vars["version"] == "1"


def test_template_vars_team_override():
    vars = _template_vars("ecom", "douyin-ops", team_override="custom-team")
    assert vars["team"] == "custom-team"


def test_init_team_generates_all_files(tmp_path):
    result = init_team("douyin", output_dir=str(tmp_path / "douyin-ops"))
    assert result["vertical"] == "douyin"
    assert result["team"] == "douyin-ops"
    expected_files = {"README.md", "cron.json", "demo_task.json", "roles.py", "team-contract.yaml"}
    assert set(result["files"]) == expected_files
    for fname in expected_files:
        assert os.path.isfile(os.path.join(str(tmp_path / "douyin-ops"), fname))


def test_init_team_copies_graphs(tmp_path):
    result = init_team("douyin", output_dir=str(tmp_path / "t"))
    assert len(result["graphs"]) >= 5
    graph_dir = os.path.join(str(tmp_path / "t"), "graphs")
    for g in result["graphs"]:
        assert os.path.isfile(os.path.join(graph_dir, g))


def test_init_team_creates_data_dirs(tmp_path):
    result = init_team("douyin", output_dir=str(tmp_path / "t"))
    data_dir = result["data_dir"]
    for sub in ["queue", "ops", "evidence", "videos", "audio", "images"]:
        assert os.path.isdir(os.path.join(data_dir, sub))


def test_init_team_contract_substituted(tmp_path):
    init_team("ecommerce", output_dir=str(tmp_path / "ecom"))
    contract_path = os.path.join(str(tmp_path / "ecom"), "team-contract.yaml")
    with open(contract_path, encoding="utf-8") as f:
        content = f.read()
    assert "team: ecommerce-ops" in content
    assert "vertical: ecommerce" in content
    assert "$team" not in content
    assert "$vertical" not in content


def test_init_team_roles_substituted(tmp_path):
    init_team("ecommerce", output_dir=str(tmp_path / "ecom"))
    roles_path = os.path.join(str(tmp_path / "ecom"), "roles.py")
    with open(roles_path, encoding="utf-8") as f:
        content = f.read()
    assert "ecommerce-ops" in content
    assert "$team" not in content


def test_init_team_cron_substituted(tmp_path):
    init_team("ecommerce", output_dir=str(tmp_path / "ecom"))
    cron_path = os.path.join(str(tmp_path / "ecom"), "cron.json")
    with open(cron_path, encoding="utf-8") as f:
        cron = json.load(f)
    assert cron["team"] == "ecommerce-ops"
    assert cron["vertical"] == "ecommerce"
    assert len(cron["crons"]) >= 4
    for c in cron["crons"]:
        assert "ecommerce-ops" in c["name"]


def test_init_team_demo_passes(tmp_path):
    result = init_team("douyin", output_dir=str(tmp_path / "demo"), demo=True)
    demo = result["demo"]
    assert demo["ok"] is True
    checks = demo["checks"]
    assert checks["demo_task_json"] == "ok"
    assert checks["roles_import"] == "ok"
    assert "ok" in checks["graphs"]
    assert checks["data_dirs"] == "ok"


def test_init_team_demo_roles_importable(tmp_path):
    import importlib.util
    result = init_team("douyin", output_dir=str(tmp_path / "demo"))
    roles_path = os.path.join(result["output_dir"], "roles.py")
    spec = importlib.util.spec_from_file_location("gen_roles", roles_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "lead" in mod.ALL_ROLES
    assert "analyst" in mod.ALL_ROLES
    assert mod.ROLE_TOOLS["lead"] == frozenset({
        "queue_ops", "publish_scheduler", "ops_health", "pattern_review", "team_contract"
    })


def test_init_team_invalid_vertical(tmp_path):
    with pytest.raises(ValueError):
        init_team("bad vertical!", output_dir=str(tmp_path / "bad"))


def test_init_team_invalid_template(tmp_path):
    with pytest.raises(FileNotFoundError):
        init_team("douyin", template_name="nonexistent", output_dir=str(tmp_path / "bad"))


def test_init_team_idempotent_rerun(tmp_path):
    out = str(tmp_path / "idem")
    init_team("douyin", output_dir=out)
    result2 = init_team("douyin", output_dir=out)
    assert set(result2["files"]) == {"README.md", "cron.json", "demo_task.json", "roles.py", "team-contract.yaml"}


def test_init_team_graphs_valid_json(tmp_path):
    result = init_team("douyin", output_dir=str(tmp_path / "g"))
    graph_dir = os.path.join(str(tmp_path / "g"), "graphs")
    for g in result["graphs"]:
        with open(os.path.join(graph_dir, g), encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)


def test_init_team_demo_task_has_restock_chain(tmp_path):
    init_team("douyin", output_dir=str(tmp_path / "dt"))
    demo_path = os.path.join(str(tmp_path / "dt"), "demo_task.json")
    with open(demo_path, encoding="utf-8") as f:
        demo = json.load(f)
    task = demo["demo_task"]
    assert task["chain"] == "restock_chain"
    assert len(task["steps"]) == 5
    assert task["steps"] == ["script", "image", "tts", "compose", "enqueue"]


def test_init_team_contract_has_slots(tmp_path):
    init_team("douyin", output_dir=str(tmp_path / "s"))
    contract_path = os.path.join(str(tmp_path / "s"), "team-contract.yaml")
    import yaml
    with open(contract_path, encoding="utf-8") as f:
        contract = yaml.safe_load(f)
    assert "slots" in contract
    slots = contract["slots"]
    assert "platform_selectors" in slots
    assert "data_sources" in slots
    assert "compliance_gates" in slots
    assert len(contract["roles"]) >= 7


def test_init_team_second_vertical_independent(tmp_path):
    r1 = init_team("douyin", output_dir=str(tmp_path / "a"))
    r2 = init_team("ecommerce", output_dir=str(tmp_path / "b"))
    with open(os.path.join(r1["output_dir"], "team-contract.yaml"), encoding="utf-8") as f:
        c1 = f.read()
    with open(os.path.join(r2["output_dir"], "team-contract.yaml"), encoding="utf-8") as f:
        c2 = f.read()
    assert "team: douyin-ops" in c1
    assert "team: ecommerce-ops" in c2
    # template name "douyin-ops" legitimately appears in 生成注释, 但 team: 字段必须不同
    assert "team: douyin-ops" not in c2
