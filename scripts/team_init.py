#!/usr/bin/env python3
# M3-9 §6.4: 团队模板化生成器.
# fusion team init <vertical> --template douyin-ops
#   → 生成: team-contract.yaml + 角色定义 + graph 目录 + 数据目录 + cron 触发器
#   → 差异项 (platform_selectors/data_sources/compliance_gates) 由模板 slots 承载
# 验收 M3 退出标准 #4: 生成 dummy 团队可跑通 demo 任务.

import argparse
import json
import logging
import os
import shutil
import sys
from string import Template

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("team_init")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "out", "teams")

# 模板默认变量 (未在 CLI 提供时用)
_TEMPLATE_DEFAULTS = {
    "version": "1",
}


def _list_templates():
    if not os.path.isdir(TEMPLATES_DIR):
        return []
    return sorted(
        d for d in os.listdir(TEMPLATES_DIR)
        if os.path.isdir(os.path.join(TEMPLATES_DIR, d)) and not d.startswith("_")
    )


def _template_vars(vertical, template_name, team_override=""):
    team = team_override or f"{vertical}-ops"
    return {
        "vertical": vertical,
        "team": team,
        "template": template_name,
        "version": _TEMPLATE_DEFAULTS["version"],
        "repo_root": REPO_ROOT,
    }


def _render_file(src_path, dst_path, variables):
    # string.Template $var 替换. 未定义 $var 保留原样 (不崩), 方便模板含 $ 字符.
    with open(src_path, encoding="utf-8") as f:
        content = f.read()
    tmpl = Template(content)
    try:
        rendered = tmpl.safe_substitute(variables)
    except (ValueError, KeyError) as e:
        logger.error("template render failed %s: %s", src_path, e)
        raise
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    logger.info("rendered %s -> %s", src_path, dst_path)


def _copy_graphs(template_dir, output_dir):
    src_graphs = os.path.join(template_dir, "graphs")
    dst_graphs = os.path.join(output_dir, "graphs")
    if not os.path.isdir(src_graphs):
        logger.warning("template has no graphs/ dir: %s", src_graphs)
        return []
    os.makedirs(dst_graphs, exist_ok=True)
    copied = []
    for name in sorted(os.listdir(src_graphs)):
        if not name.endswith(".json"):
            continue
        shutil.copy2(os.path.join(src_graphs, name), os.path.join(dst_graphs, name))
        copied.append(name)
    logger.info("copied %d graphs -> %s", len(copied), dst_graphs)
    return copied


def _create_data_dirs(output_dir):
    data_dir = os.path.join(output_dir, "data")
    subdirs = ["queue", "ops", "evidence", "videos", "audio", "images"]
    for sub in subdirs:
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
    logger.info("created data dirs under %s", data_dir)
    return data_dir


def _render_all(template_dir, output_dir, variables):
    # 渲染所有 .tmpl 文件 (去 .tmpl 后缀). 非 .tmpl 文件跳过.
    written = []
    for name in sorted(os.listdir(template_dir)):
        src = os.path.join(template_dir, name)
        if not os.path.isfile(src) or not name.endswith(".tmpl"):
            continue
        dst_name = name[:-len(".tmpl")]
        dst = os.path.join(output_dir, dst_name)
        _render_file(src, dst, variables)
        written.append(dst_name)
    return written


def init_team(vertical, template_name="douyin-ops", output_dir="", team_override="", demo=False):
    # 主生成逻辑. 返回生成结果 dict (供测试 + CLI 汇总).
    if not vertical or not vertical.replace("-", "").isalnum():
        raise ValueError(f"invalid vertical name: {vertical!r} (alnum + dash only)")
    template_dir = os.path.join(TEMPLATES_DIR, template_name)
    if not os.path.isdir(template_dir):
        avail = _list_templates()
        raise FileNotFoundError(
            f"template not found: {template_name} (available: {avail})"
        )

    out = output_dir or os.path.join(DEFAULT_OUTPUT, f"{vertical}-ops")
    if os.path.exists(out) and os.listdir(out):
        logger.warning("output dir not empty, will overwrite files: %s", out)
    os.makedirs(out, exist_ok=True)

    variables = _template_vars(vertical, template_name, team_override)

    logger.info("team_init: vertical=%s template=%s -> %s", vertical, template_name, out)
    rendered = _render_all(template_dir, out, variables)
    graphs = _copy_graphs(template_dir, out)
    data_dir = _create_data_dirs(out)

    result = {
        "vertical": vertical,
        "team": variables["team"],
        "template": template_name,
        "output_dir": out,
        "files": rendered,
        "graphs": graphs,
        "data_dir": data_dir,
    }

    if demo:
        result["demo"] = _run_demo(out, variables)

    logger.info("team_init done: %d files + %d graphs + data dirs", len(rendered), len(graphs))
    return result


def _run_demo(output_dir, variables):
    # M3 验收 #4: dummy 团队跑通 demo 任务.
    # 验证生成包结构完整 + demo_task.json 可解析 + contract YAML 可读 + roles 可 import.
    demo_path = os.path.join(output_dir, "demo_task.json")
    contract_path = os.path.join(output_dir, "team-contract.yaml")
    roles_path = os.path.join(output_dir, "roles.py")
    graph_dir = os.path.join(output_dir, "graphs")

    demo_result = {"checks": {}, "ok": True}

    # 1. demo_task.json 解析
    try:
        with open(demo_path, encoding="utf-8") as f:
            demo_data = json.load(f)
        demo_task = demo_data.get("demo_task", {})
        assert demo_task.get("id"), "demo_task.id missing"
        assert demo_task.get("chain") == "restock_chain", "chain not restock_chain"
        demo_result["checks"]["demo_task_json"] = "ok"
    except Exception as e:  # noqa: BLE001 (demo probe must not crash)
        demo_result["checks"]["demo_task_json"] = f"fail: {e}"
        demo_result["ok"] = False

    # 2. contract YAML 存在 + team 字段正确
    try:
        import yaml
        with open(contract_path, encoding="utf-8") as f:
            contract = yaml.safe_load(f)
        assert contract["team"] == variables["team"], f"team mismatch: {contract.get('team')}"
        assert contract["vertical"] == variables["vertical"], "vertical mismatch"
        assert "roles" in contract and len(contract["roles"]) >= 7, "roles incomplete"
        demo_result["checks"]["contract_yaml"] = "ok"
    except ImportError:
        demo_result["checks"]["contract_yaml"] = "skip (pyyaml not installed)"
    except Exception as e:  # noqa: BLE001 (demo probe must not crash)
        demo_result["checks"]["contract_yaml"] = f"fail: {e}"
        demo_result["ok"] = False

    # 3. roles.py 可 import
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("team_roles", roles_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert len(mod.ALL_ROLES) >= 7, f"roles count {len(mod.ALL_ROLES)} < 7"
        assert mod.LEAD in mod.ROLE_TOOLS, "LEAD missing in ROLE_TOOLS"
        demo_result["checks"]["roles_import"] = "ok"
    except Exception as e:  # noqa: BLE001 (demo probe must not crash)
        demo_result["checks"]["roles_import"] = f"fail: {e}"
        demo_result["ok"] = False

    # 4. graphs 目录有 5 mini-graphs
    try:
        graph_files = [f for f in os.listdir(graph_dir) if f.endswith(".json")]
        assert len(graph_files) >= 5, f"graphs count {len(graph_files)} < 5"
        for gf in graph_files:
            with open(os.path.join(graph_dir, gf), encoding="utf-8") as f:
                json.load(f)
        demo_result["checks"]["graphs"] = f"ok ({len(graph_files)} graphs)"
    except Exception as e:  # noqa: BLE001 (demo probe must not crash)
        demo_result["checks"]["graphs"] = f"fail: {e}"
        demo_result["ok"] = False

    # 5. data dirs 存在
    data_dir = os.path.join(output_dir, "data")
    try:
        for sub in ["queue", "ops", "evidence"]:
            assert os.path.isdir(os.path.join(data_dir, sub)), f"missing data/{sub}"
        demo_result["checks"]["data_dirs"] = "ok"
    except Exception as e:  # noqa: BLE001 (demo probe must not crash)
        demo_result["checks"]["data_dirs"] = f"fail: {e}"
        demo_result["ok"] = False

    status = "PASS" if demo_result["ok"] else "FAIL"
    logger.info("demo check: %s — %s", status, demo_result["checks"])
    return demo_result


def main():
    parser = argparse.ArgumentParser(
        prog="fusion team",
        description="M3-9 团队模板化生成器 (§6.4)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成团队包")
    p_init.add_argument("vertical", help="垂直名称 (如 douyin / ecommerce)")
    p_init.add_argument("--template", default="douyin-ops", help="模板名 (默认 douyin-ops)")
    p_init.add_argument("--output-dir", default="", help="输出目录 (默认 out/teams/<vertical>-ops)")
    p_init.add_argument("--team", default="", help="团队名覆盖 (默认 <vertical>-ops)")
    p_init.add_argument("--demo", action="store_true", help="生成后跑 demo 验收 (M3 退出标准 #4)")

    p_list = sub.add_parser("list-templates", help="列出可用模板")
    p_list.add_argument("--template", default="", help="(unused)")

    args = parser.parse_args()

    if args.command == "init":
        try:
            result = init_team(
                vertical=args.vertical,
                template_name=args.template,
                output_dir=args.output_dir,
                team_override=args.team,
                demo=args.demo,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            if args.demo and not result.get("demo", {}).get("ok", False):
                sys.exit(1)
        except (ValueError, FileNotFoundError) as e:
            logger.error("%s", e)
            sys.exit(2)
    elif args.command == "list-templates":
        templates = _list_templates()
        print(json.dumps({"templates": templates}, indent=2))
        if not templates:
            sys.exit(1)


if __name__ == "__main__":
    main()
