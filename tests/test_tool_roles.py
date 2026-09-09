import inspect
import pkgutil

import pytest

import douyin_tools
from tools.base import BaseTool
from douyin_tools.roles import ALL_ROLES, ROLE_TOOLS, LEAD, RESEARCHER, WRITER, IMAGER, VOICER, PUBLISHER, ANALYST


def _discover_tool_classes():
    skip = {"config", "script_helpers", "comfyui_client", "__init__", "roles", "playwright_profile"}
    classes = []
    for _, name, _ in pkgutil.iter_modules(douyin_tools.__path__):
        if name in skip:
            continue
        mod = __import__(f"douyin_tools.{name}", fromlist=["*"])
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ == f"douyin_tools.{name}" and issubclass(obj, BaseTool) and obj is not BaseTool:
                classes.append(obj)
    return classes


def test_all_tools_have_roles():
    classes = _discover_tool_classes()
    assert len(classes) == 20, f"expected 20 tool classes, got {len(classes)}"
    missing = [c.__name__ for c in classes if not hasattr(c, "roles") or not c.roles]
    assert not missing, f"tools without roles: {missing}"


def test_roles_are_valid():
    classes = _discover_tool_classes()
    for cls in classes:
        invalid = set(cls.roles) - ALL_ROLES
        assert not invalid, f"{cls.__name__}.roles has invalid values: {invalid}"


def test_role_tool_mapping_consistent():
    classes = {cls.name: cls for cls in _discover_tool_classes()}
    for role, expected_tools in ROLE_TOOLS.items():
        for tool_name in expected_tools:
            assert tool_name in classes, f"ROLE_TOOLS[{role}] references missing tool: {tool_name}"
            assert role in classes[tool_name].roles, (
                f"ROLE_TOOLS[{role}] lists {tool_name} but {tool_name}.roles={classes[tool_name].roles} lacks '{role}'"
            )


def test_role_tool_mapping_no_stale():
    classes = {cls.name: cls for cls in _discover_tool_classes()}
    for tool_name, cls in classes.items():
        for role in cls.roles:
            assert tool_name in ROLE_TOOLS.get(role, frozenset()), (
                f"{tool_name}.roles contains '{role}' but ROLE_TOOLS[{role}] does not list {tool_name}"
            )


def test_every_role_has_tools():
    for role in ALL_ROLES:
        assert len(ROLE_TOOLS.get(role, frozenset())) > 0, f"role '{role}' has no tools"


def test_researcher_cannot_publish():
    from douyin_tools.douyin_publish import DouyinPublishTool
    assert RESEARCHER not in DouyinPublishTool.roles


def test_publisher_cannot_analyze():
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    assert PUBLISHER not in PerfAnalyzeTool.roles


def test_lead_has_planning_visibility_only():
    lead_tools = ROLE_TOOLS[LEAD]
    assert lead_tools == frozenset({"queue_ops", "publish_scheduler", "ops_health"}), (
        f"LEAD tools drifted: {lead_tools}"
    )


def test_shared_tools_visible_to_both_roles():
    from douyin_tools.web_image_search import WebImageSearchTool
    assert RESEARCHER in WebImageSearchTool.roles
    assert IMAGER in WebImageSearchTool.roles

    from douyin_tools.douyin_reply import DouyinReplyTool
    assert PUBLISHER in DouyinReplyTool.roles
    assert ANALYST in DouyinReplyTool.roles


def test_role_isolation_no_cross_contamination():
    classes = {cls.name: cls for cls in _discover_tool_classes()}
    producer_roles = {RESEARCHER, WRITER, IMAGER, VOICER}
    for tool_name, cls in classes.items():
        if tool_name in {"douyin_stats", "perf_track", "perf_analyze", "self_repair", "ops_report", "mlx_reply_draft"}:
            for r in producer_roles:
                assert r not in cls.roles, f"analyst tool {tool_name} leaked producer role {r}"
