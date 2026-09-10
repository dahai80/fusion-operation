import logging
import os

logger = logging.getLogger(__name__)

# M3-8 §6.6: 凭据入 keychain. MLX API key 优先从 keyring (macOS Keychain) 读,
# 回退 env > settings.json (config._resolve_mlx_api_key 已覆盖后两者).
# keyring 不可用 (headless/CI) 时静默降级, 不阻断主路径.

_SERVICE = "fusion-operation"
_KEY_NAME = "mlx_api_key"

try:
    import keyring

    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False
    logger.info("credential_store: keyring not installed, credentials stay in env/settings")


def get_credential(name=_KEY_NAME, fallback=""):
    # 读凭据: keyring > fallback (调用方传 env/settings 解析结果).
    if not _HAS_KEYRING:
        return fallback
    try:
        val = keyring.get_password(_SERVICE, name)
        if val:
            logger.debug("credential_store: hit keyring name=%s", name)
            return val
    except Exception as e:
        logger.warning("credential_store: keyring get failed name=%s: %s", name, e)
    return fallback


def set_credential(name, value):
    # 写凭据到 keyring. 部署时 `fusion team init` 或手动调用.
    if not _HAS_KEYRING:
        logger.warning("credential_store: keyring not installed, cannot set name=%s", name)
        return False
    try:
        keyring.set_password(_SERVICE, name, value)
        logger.info("credential_store: stored name=%s in keyring", name)
        return True
    except Exception as e:
        logger.error("credential_store: keyring set failed name=%s: %s", name, e)
        return False


def delete_credential(name=_KEY_NAME):
    if not _HAS_KEYRING:
        return False
    try:
        keyring.delete_password(_SERVICE, name)
        logger.info("credential_store: deleted name=%s", name)
        return True
    except Exception:
        return False


def get_mlx_api_key(env_val="", settings_val=""):
    # MLX API key 解析优先级: env > keyring > settings.json.
    # env_val / settings_val 由 config 传入 (已解析), keyring 兜底.
    if env_val:
        return env_val
    kr = get_credential(_KEY_NAME, fallback="")
    if kr:
        return kr
    return settings_val
