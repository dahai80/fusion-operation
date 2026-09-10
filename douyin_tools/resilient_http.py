import asyncio
import logging
import random
import time

import httpx

from . import config
from .config import mlx_headers
from .history_store import append_alert

logger = logging.getLogger(__name__)

# M3-7 §6.5: LLM/ComfyUI 调用统一重试 (3 次指数退避) + 熔断 (连续 N 次失败挂起角色 + team.alert).
# 所有 mlx/comfyui HTTP POST 经 post_json 统一入口, 不再裸 client.post.

_RETRIES_DEFAULT = 3
_BACKOFF_BASE = 1.0
_THRESHOLD_DEFAULT = 5
_COOLDOWN_SEC = 60.0

_RETRIABLE_STATUS = {500, 502, 503, 504}
_RETRIABLE_EXC = (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def _resilience_cfg():
    # 读 douyin_config.json resilience 段, 缺失用默认.
    try:
        raw = config.get_json_config().get("resilience") or {}
    except Exception as e:
        logger.warning("resilient_http config read failed: %s", e)
        return {}
    return raw


class CircuitBreaker:
    # 每服务 (mlx/comfyui) 一个熔断器. 连续失败达 threshold → open (cooldown).
    # open 期间请求直接拒绝 (不发 HTTP). cooldown 过后 half-open: 放一个请求探活.

    def __init__(self, service, threshold=_THRESHOLD_DEFAULT, cooldown=_COOLDOWN_SEC):
        self.service = service
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._state = "closed"  # closed | open | half_open
        self._open_until = 0.0

    def is_open(self):
        if self._state == "open":
            if time.time() >= self._open_until:
                self._state = "half_open"
                logger.info("circuit %s half_open (cooldown elapsed)", self.service)
                return False
            return True
        return False

    def record_success(self):
        if self._state != "closed":
            logger.info("circuit %s closed (recovered)", self.service)
        self._failures = 0
        self._state = "closed"
        self._open_until = 0.0

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.threshold and self._state != "open":
            self._state = "open"
            self._open_until = time.time() + self.cooldown
            alert = {
                "type": "circuit_open",
                "service": self.service,
                "failures": self._failures,
                "threshold": self.threshold,
                "cooldown_sec": self.cooldown,
                "ts": int(time.time()),
                "msg": f"circuit breaker open: {self.service} consecutive {self._failures} failures, role suspended",
            }
            append_alert(alert)
            logger.error("circuit %s OPEN: %d failures (cooldown %.0fs)", self.service, self._failures, self.cooldown)
        else:
            logger.warning("circuit %s failure %d/%d", self.service, self._failures, self.threshold)

    def reset(self):
        self._failures = 0
        self._state = "closed"
        self._open_until = 0.0

    @property
    def state(self):
        return self._state

    @property
    def failures(self):
        return self._failures


_BREAKERS = {}


def get_breaker(service):
    # 模块级单例熔断器. 测试可 reset_breakers() 重置.
    if service not in _BREAKERS:
        cfg = _resilience_cfg()
        threshold = int(cfg.get("circuit_threshold", _THRESHOLD_DEFAULT))
        cooldown = float(cfg.get("circuit_cooldown_sec", _COOLDOWN_SEC))
        _BREAKERS[service] = CircuitBreaker(service, threshold=threshold, cooldown=cooldown)
    return _BREAKERS[service]


def reset_breakers():
    # 测试用: 清空所有熔断器状态.
    _BREAKERS.clear()


class CircuitOpenError(Exception):
    pass


async def post_json(url, json, *, service="mlx", headers=None, timeout=None, retries=None):
    # 统一 HTTP POST 入口: 重试 (3 次指数退避+jitter) + 熔断 (per-service).
    # 成功 → 返回 httpx.Response. 熔断 open → raise CircuitOpenError.
    # 重试耗尽 → raise 最后一个异常 (或返回 5xx Response 由调用方判 status).
    cfg = _resilience_cfg()
    if retries is None:
        retries = int(cfg.get("max_retries", _RETRIES_DEFAULT))
    backoff_base = float(cfg.get("backoff_base_sec", _BACKOFF_BASE))
    if timeout is None:
        timeout = config.MLX_TIMEOUT
    if headers is None:
        headers = mlx_headers()

    breaker = get_breaker(service)
    if breaker.is_open():
        logger.warning("post_json rejected: circuit %s open", service)
        raise CircuitOpenError(f"circuit open: {service}")

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                resp = await client.post(url, json=json)
            if resp.status_code in _RETRIABLE_STATUS and attempt < retries:
                breaker.record_failure()
                delay = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                logger.warning("post_json %s http %d (attempt %d/%d) retry in %.1fs", service, resp.status_code, attempt, retries, delay)
                await asyncio.sleep(delay)
                last_exc = None
                continue
            if resp.status_code < 400:
                breaker.record_success()
            else:
                breaker.record_failure()
            return resp
        except _RETRIABLE_EXC as exc:
            last_exc = exc
            breaker.record_failure()
            if attempt < retries:
                delay = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                logger.warning("post_json %s exc %s (attempt %d/%d) retry in %.1fs", service, type(exc).__name__, attempt, retries, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("post_json %s exhausted %d retries: %s", service, retries, exc)
    if last_exc:
        raise last_exc
    # 5xx 且重试耗尽: 返回最后的 resp (调用方自行处理)
    return resp
