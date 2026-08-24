import importlib
import inspect
import logging
import pkgutil

from tools.registry import ToolRegistry
from tools.base import BaseTool

logger = logging.getLogger(__name__)

# H8: 注册改自动扫描. 旧实现手工列 19 import + 19 register + _PENDING_TOOLS 空列表,
# 加新工具要同步改 4 处 (__init__/__all__/register/sync_plugins) 易漏 (ops_health
# 导入了却没 register 也没 sync 就是漏改的证据). 扫描包内所有 BaseTool 子类作单一源.
# 排除非工具模块: config/script_helpers/comfyui_client 是公共库非工具.
_NON_TOOL_MODULES = {"config", "script_helpers", "comfyui_client"}

__all__ = ["create_douyin_registry"]


def _discover_tools():
    # 扫 douyin_tools 包, 返回 [(module_name, ToolClass), ...] 按 module 名排序保证确定性.
    tools = []
    for mod_info in pkgutil.iter_modules(__path__, __name__ + "."):
        mod_short = mod_info.name.split(".")[-1]
        if mod_short.startswith("_") or mod_short in _NON_TOOL_MODULES:
            continue
        try:
            mod = importlib.import_module(mod_info.name)
        except Exception as e:
            logger.exception("douyin_tools import failed module=%s: %s", mod_short, e)
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is BaseTool:
                continue
            if issubclass(obj, BaseTool) and obj.__module__ == mod_info.name:
                tools.append((mod_short, obj))
    tools.sort(key=lambda t: t[0])
    return tools


_DISCOVERED = _discover_tools()

# 导出工具类到包顶层 (兼容 from douyin_tools import XxxTool 旧用法).
for _mod_short, _cls in _DISCOVERED:
    globals()[_cls.__name__] = _cls
    __all__.append(_cls.__name__)


def create_douyin_registry() -> ToolRegistry:
    registry = ToolRegistry()
    failed = []
    for mod_short, cls in _DISCOVERED:
        try:
            registry.register(cls())
        except Exception as e:
            logger.exception("douyin tools register failed cls=%s: %s", cls.__name__, e)
            failed.append(cls.__name__)
    if failed:
        logger.warning("douyin tools registered count=%d, failed=%s", registry.count, failed)
    else:
        logger.info("douyin tools registered count=%d", registry.count)
    return registry
