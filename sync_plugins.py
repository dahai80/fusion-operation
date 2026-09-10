import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(os.path.expanduser("~/.fusion-agent-studio/plugins"))

# M3-8: 去个人路径化. 默认相对本仓根, env 覆盖.
_REPO_ROOT = Path(os.path.dirname(os.path.abspath(__file__)))

SHIM_TEMPLATE = '''\
import logging
import os
import sys

logger = logging.getLogger(__name__)

# daemon 运行在 agent-studio 自己的 .venv, fusion-operation 装在 monorepo 共享 venv.
# shim import 前把 fusion-operation 源码目录注入 sys.path, 保证 douyin_tools 可 import.
# 候选顺序: 环境变量 > 仓根 (sync_plugins 生成时注入).
_FUSION_OP_DIR = (
    os.environ.get("FUSION_OPERATION_DIR")
    or {repo_root!r}
)
if _FUSION_OP_DIR not in sys.path:
    sys.path.insert(0, _FUSION_OP_DIR)
    logger.info("douyin shim inject sys.path += %s", _FUSION_OP_DIR)

from douyin_tools.{mod} import {cls} as _Tool

{cls} = _Tool
logger.info("douyin plugin shim loaded: {name} -> %s", _Tool.__name__)'''
'''


def _discover_tools():
    # H8: 与 douyin_tools/__init__.py 同源扫描, 不再手工列 TOOLS 列表.
    # 旧列表漏 ops_health (导入了却没 sync) 就是手工维护漂移的证据.
    # 返回 [(module_short, ClassName, tool_name), ...].
    import importlib
    import inspect
    import pkgutil

    import douyin_tools
    from tools.base import BaseTool

    non_tool = {"config", "script_helpers", "comfyui_client"}
    tools = []
    for mod_info in pkgutil.iter_modules(douyin_tools.__path__, "douyin_tools."):
        mod_short = mod_info.name.split(".")[-1]
        if mod_short.startswith("_") or mod_short in non_tool:
            continue
        try:
            mod = importlib.import_module(mod_info.name)
        except Exception as e:
            logger.exception("sync_plugins import failed module=%s: %s", mod_short, e)
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is BaseTool:
                continue
            if issubclass(obj, BaseTool) and obj.__module__ == mod_info.name:
                tool_name = getattr(obj, "name", mod_short)
                tools.append((mod_short, obj.__name__, tool_name))
    tools.sort(key=lambda t: t[2])
    return tools


def sync():
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    tools = _discover_tools()
    written = []
    for mod, cls, name in tools:
        path = PLUGIN_DIR / f"douyin_{name}.py"
        path.write_text(SHIM_TEMPLATE.format(mod=mod, cls=cls, name=name, repo_root=str(_REPO_ROOT)))
        written.append(name)
    logger.info("sync_plugins wrote %d shims to %s", len(written), PLUGIN_DIR)
    for w in written:
        logger.info("  - douyin_%s.py", w)
    print(f"sync_plugins: wrote {len(written)} plugin shims to {PLUGIN_DIR}")
    return written


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sync()
