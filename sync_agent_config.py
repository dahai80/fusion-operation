import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sync_agent_config")

OPERATION_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get(
    "DOUYIN_CONFIG_FILE", os.path.join(OPERATION_DIR, "douyin_config.json")
)


def _load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_ref(cfg, ref):
    # 引用式配置值： "models.chat" → cfg["models"]["chat"]。
    if not isinstance(ref, str) or "." not in ref:
        return ref
    parts = ref.split(".")
    cur = cfg
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            logger.warning("tool_config ref %r 解析失败，缺段 %r，跳过", ref, p)
            return None
        cur = cur[p]
    return cur


def build_tool_configs(cfg):
    # 从 agent_studio.tool_config 构建 {tool_name: {param: value}}。
    # 只保留能解析的引用；None 值跳过（避免注入空参数）。
    agent_cfg = cfg.get("agent_studio", {}) or {}
    raw = agent_cfg.get("tool_config", {}) or {}
    out = {}
    for tool_name, params in raw.items():
        if not isinstance(params, dict):
            logger.warning("tool_config[%s] 非 dict，跳过", tool_name)
            continue
        resolved = {}
        for param, ref in params.items():
            val = _resolve_ref(cfg, ref)
            if val is not None:
                resolved[param] = val
            else:
                logger.warning("tool_config[%s].%s 引用 %r 解析为 None，跳过", tool_name, param, ref)
        if resolved:
            out[tool_name] = resolved
    return out


def build_definition(manifest, tool_configs):
    # 生成 AgentDefinition.to_dict 兼容的 definition.json。
    # tools 用对象格式 {name, type, config}，daemon graph.execute 读
    # definition.json → set_tool_configs → _merge_tool_config_defaults 注入。
    tools_out = []
    for t in manifest.get("tools", []):
        name = t if isinstance(t, str) else t.get("name", "")
        cfg = tool_configs.get(name, {})
        tools_out.append({
            "name": name,
            "type": "function",
            "description": "",
            "endpoint": "",
            "config": cfg,
        })
    model_name = manifest.get("model", "")
    definition = {
        "$schema": "https://fusion.dev/agent-definition-v1.json",
        "schema_version": "1.0.0",
        "agent_id": "",
        "name": manifest.get("name", ""),
        "version": manifest.get("version", "0.1.0"),
        "description": manifest.get("description", ""),
        "system_prompt": manifest.get("system_prompt", ""),
        "model": {
            "provider": "mlx",
            "model_name": model_name,
            "temperature": manifest.get("temperature", 0.7),
            "max_tokens": manifest.get("max_tokens", 4096),
            "top_p": manifest.get("top_p", 1.0),
            "context_window": manifest.get("context_window", 32768),
        },
        "tools": tools_out,
        "knowledge": {"enable_rag": False, "kb_id": "", "top_k": 5, "strategy": "hybrid"},
        "orchestration": {},
        "metadata": {
            "author": manifest.get("author", ""),
            "tags": manifest.get("tags", []),
            "published_at": manifest.get("published_at"),
        },
        "context_injection": {},
        "artifact_policy": {},
        "permissions": {},
        "status": manifest.get("status", "draft"),
    }
    return definition


def sync():
    cfg = _load_config()
    agent_cfg = cfg.get("agent_studio", {}) or {}
    agent_id = agent_cfg.get("agent_id", "")
    if not agent_id:
        logger.error("agent_studio.agent_id 未配置，无法 sync definition.json")
        return 1
    agent_dir = os.path.expanduser(
        f"~/.fusion-agent-studio/agents/{agent_id}"
    )
    manifest_path = os.path.join(agent_dir, ".fusion-agent", "manifest.json")
    if not os.path.exists(manifest_path):
        logger.error("manifest 不存在: %s", manifest_path)
        return 1
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    tool_configs = build_tool_configs(cfg)
    definition = build_definition(manifest, tool_configs)
    definition["agent_id"] = agent_id
    defn_path = os.path.join(agent_dir, "definition.json")
    with open(defn_path, "w", encoding="utf-8") as f:
        json.dump(definition, f, ensure_ascii=False, indent=2)
    logger.info(
        "sync definition.json ok agent_id=%s tools=%d configs=%d -> %s",
        agent_id,
        len(definition["tools"]),
        len(tool_configs),
        defn_path,
    )
    for name, tc in tool_configs.items():
        logger.info("  tool %s config=%s", name, tc)
    return 0


if __name__ == "__main__":
    sys.exit(sync())
