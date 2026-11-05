import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_CACHE = {"jwt": "", "expires_at": 0}

# Refresh 60s before expiry so a long-lived caller never sends an expiring token.
_EXPIRY_MARGIN = 60


def _module_key():
    # Same source order as the daemon-side static_module_key:
    # env FUSION_AGENT_STUDIO_API_KEY > persisted ~/.fusion-agent-studio/web_module_key.
    env_key = os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return (Path.home() / ".fusion-agent-studio" / "web_module_key").read_text().strip()
    except OSError:
        return ""


async def _ensure_token(client):
    if _TOKEN_CACHE["jwt"] and time.time() < _TOKEN_CACHE["expires_at"] - _EXPIRY_MARGIN:
        return _TOKEN_CACHE["jwt"]
    key = _module_key()
    if not key:
        raise RuntimeError(
            "no daemon module key: set FUSION_AGENT_STUDIO_API_KEY or ~/.fusion-agent-studio/web_module_key"
        )
    # auth.login is on the daemon PRE_AUTH_ALLOWLIST, so this call needs no token.
    res = await client.call("auth.login", {"apikey": key})
    if res.get("error"):
        raise RuntimeError(f"auth.login failed: {res['error']}")
    _TOKEN_CACHE["jwt"] = str(res.get("jwt", ""))
    _TOKEN_CACHE["expires_at"] = int(res.get("expires_at", 0))
    logger.info("rpc_auth: logged in key_id=%s expires_at=%s", res.get("key_id", ""), _TOKEN_CACHE["expires_at"])
    return _TOKEN_CACHE["jwt"]


async def authed_call(method, params, socket_path, timeout=None):
    # AgentClient.call wrapper injecting the _auth JWT required by the daemon
    # #326 UDS auth enforcement (401 "missing _auth" without it). Login via
    # auth.login with the static module key, cache until near expiry, retry
    # once on 401 in case the daemon rotated its secret.
    from agent_runtime.sdk.client import AgentClient

    kwargs = {"socket_path": socket_path}
    if timeout is not None:
        kwargs["default_timeout"] = timeout
    client = AgentClient(**kwargs)
    token = await _ensure_token(client)
    payload = dict(params)
    payload["_auth"] = {"jwt": token}
    res = await client.call(method, payload)
    if res.get("error") and "Auth required" in str(res["error"]):
        _TOKEN_CACHE["jwt"] = ""
        _TOKEN_CACHE["expires_at"] = 0
        token = await _ensure_token(client)
        payload["_auth"] = {"jwt": token}
        res = await client.call(method, payload)
    return res


def reset_token_cache():
    # Test hook: clear cached credentials between scenarios.
    _TOKEN_CACHE["jwt"] = ""
    _TOKEN_CACHE["expires_at"] = 0
