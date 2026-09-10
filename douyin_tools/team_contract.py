import json
import logging
import os
import time
import uuid

import yaml
from tools.base import BaseTool

from .config import OPS_DATA_DIR, ensure_dirs

logger = logging.getLogger(__name__)

# M3-6 §6.3: team-contract.yaml 文档即契约. Lead 规划轮强制读取.
# 契约是 GUI 可渲染结构化文件 (YAML), Lead 行为边界写在文档里.
CONTRACT_FILE = os.environ.get(
    "DOUYIN_TEAM_CONTRACT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "team-contract.yaml"),
)
PLAN_DIR = os.path.join(OPS_DATA_DIR, "plans")

_REQUIRED_TOP = ("team", "version", "roles", "resources", "sla", "data_contract", "idempotency", "interventions", "exit_criteria")
_REQUIRED_ROLE_KEYS = ("name", "tools", "duty")


def load_contract(path=None):
    # 读 team-contract.yaml → dict. Lead 规划轮 + GUI 渲染共用此入口.
    p = path or CONTRACT_FILE
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        logger.info("team_contract loaded team=%s version=%s path=%s", data.get("team"), data.get("version"), p)
        return data or {}
    except FileNotFoundError:
        logger.warning("team_contract not found path=%s", p)
        return {}
    except (yaml.YAMLError, OSError) as e:
        logger.error("team_contract parse failed path=%s: %s", p, e)
        return {}


def validate_contract(path=None):
    # 校验契约结构完整性: 必填 top 键 + 每角色 name/tools/duty.
    data = load_contract(path)
    if not data:
        return {"valid": False, "errors": ["contract file empty or not found"]}
    errors = []
    for key in _REQUIRED_TOP:
        if key not in data:
            errors.append(f"missing top-level key: {key}")
    roles = data.get("roles") or []
    if not isinstance(roles, list) or not roles:
        errors.append("roles missing or empty")
    else:
        for i, r in enumerate(roles):
            for rk in _REQUIRED_ROLE_KEYS:
                if rk not in r:
                    errors.append(f"role[{i}] missing key: {rk}")
            tools = r.get("tools")
            if tools is not None and not isinstance(tools, list):
                errors.append(f"role[{i}].tools not a list")
    return {"valid": len(errors) == 0, "errors": errors, "team": data.get("team"), "version": data.get("version")}


def _plan_path(plan_id):
    return os.path.join(PLAN_DIR, f"{plan_id}.json")


def _write_plan(plan):
    os.makedirs(PLAN_DIR, exist_ok=True)
    p = _plan_path(plan["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    logger.info("team_contract wrote plan id=%s status=%s", plan["id"], plan["status"])


def _load_plan(plan_id):
    p = _plan_path(plan_id)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError) as e:
        logger.warning("team_contract load plan id=%s failed: %s", plan_id, e)
        return None


def _list_plans():
    if not os.path.isdir(PLAN_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(PLAN_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(PLAN_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except (ValueError, OSError):
            continue
    return out


class TeamContractTool(BaseTool):
    name = "team_contract"
    description = "团队契约门禁: read 读取 team-contract.yaml / validate 校验结构 / plan_draft Lead 起草周计划(待批准) / plan_list / plan_approve / plan_reject. Lead 规划轮强制 read+validate"
    roles = frozenset({"lead"})  # M3-1: role-based (§6.1). plan_approve/reject 亦 lead (GUI human 经 lead 通道)

    parameters = {
        "action": {"type": "string", "description": "read|validate|plan_draft|plan_list|plan_approve|plan_reject"},
        "plan_summary": {"type": "string", "description": "plan_draft 时周计划摘要 (本周目标/任务清单)"},
        "plan_id": {"type": "string", "description": "plan_approve/plan_reject 时指定计划 id"},
        "comment": {"type": "string", "description": "plan_reject 时驳回理由 (进 evidence)"},
    }

    async def execute(self, action="read", plan_summary="", plan_id="", comment="", **_):
        ensure_dirs()
        if action == "read":
            return self._read()
        if action == "validate":
            return self._validate()
        if action == "plan_draft":
            return self._plan_draft(plan_summary)
        if action == "plan_list":
            return self._plan_list()
        if action == "plan_approve":
            return self._plan_approve(plan_id)
        if action == "plan_reject":
            return self._plan_reject(plan_id, comment)
        logger.error("team_contract unknown action=%s", action)
        return json.dumps({"error": f"unknown action {action}"})

    def _read(self):
        data = load_contract()
        if not data:
            return json.dumps({"error": "contract not found or empty"})
        return json.dumps({"status": "ok", "contract": data}, ensure_ascii=False)

    def _validate(self):
        result = validate_contract()
        return json.dumps(result, ensure_ascii=False)

    def _plan_draft(self, summary):
        # Lead 起草周计划: status=pending_approval. 未批准不可执行 (契约 interventions.weekly_plan).
        if not summary:
            return json.dumps({"error": "plan_summary required"})
        ts = int(time.time())
        plan_id = f"weekly_{ts}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        plan = {
            "id": plan_id,
            "type": "weekly_plan",
            "team": "douyin-ops",
            "summary": summary,
            "status": "pending_approval",
            "drafted_at": ts,
            "approved_at": None,
            "rejected_at": None,
            "comment": None,
        }
        _write_plan(plan)
        return json.dumps({"status": "drafted", "plan_id": plan_id, "plan_status": "pending_approval"}, ensure_ascii=False)

    def _plan_list(self):
        plans = _list_plans()
        return json.dumps({"status": "ok", "plans": plans, "count": len(plans)}, ensure_ascii=False)

    def _plan_approve(self, plan_id):
        if not plan_id:
            return json.dumps({"error": "plan_id required"})
        plan = _load_plan(plan_id)
        if plan is None:
            return json.dumps({"error": "plan not found"})
        if plan["status"] != "pending_approval":
            return json.dumps({"error": f"plan status={plan['status']} not pending_approval"})
        plan["status"] = "approved"
        plan["approved_at"] = int(time.time())
        _write_plan(plan)
        logger.info("team_contract plan approved id=%s", plan_id)
        return json.dumps({"status": "approved", "plan_id": plan_id}, ensure_ascii=False)

    def _plan_reject(self, plan_id, comment):
        if not plan_id:
            return json.dumps({"error": "plan_id required"})
        plan = _load_plan(plan_id)
        if plan is None:
            return json.dumps({"error": "plan not found"})
        if plan["status"] != "pending_approval":
            return json.dumps({"error": f"plan status={plan['status']} not pending_approval"})
        plan["status"] = "rejected"
        plan["rejected_at"] = int(time.time())
        plan["comment"] = comment
        _write_plan(plan)
        logger.warning("team_contract plan rejected id=%s comment=%s", plan_id, comment)
        return json.dumps({"status": "rejected", "plan_id": plan_id, "comment": comment}, ensure_ascii=False)
