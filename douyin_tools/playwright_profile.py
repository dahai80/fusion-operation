import asyncio
import datetime as _dt
import fcntl
import logging
import os
import signal
import subprocess
from contextlib import contextmanager

from .config import DOUYIN_PROFILE

logger = logging.getLogger(__name__)


@contextmanager
def profile_lock():
    # 审计 0908 F1: 5 个 Playwright 工具 (publish/self_comment/stats/fetch/reply) 共用 DOUYIN_PROFILE
    # 持久目录, launch_persistent_context 对同目录独占. cron 时间线互相碰撞时 _kill_stale_profile_chrome
    # 按进程 age SIGTERM 活跃任务 ("互杀活跃任务"). 全局 flock 串行化所有 Playwright 操作: 持锁者独占
    # profile, 其余进程阻塞等待. 消灭 "按进程存活时长猜锁主" 的错误抽象 (F1 根因).
    # 锁跨整个浏览器会话 (launch->work->close), 非仅 launch 阶段 —— 否则 release 后并发进程 kill 仍误杀
    # 活跃 ctx. 进程崩溃内核自动释放 flock, 下一进程 acquire 后 kill_stale 清孤儿 chrome (崩溃恢复).
    lock_path = DOUYIN_PROFILE + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock = open(lock_path, "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        logger.info("profile_lock acquired %s pid=%d", lock_path, os.getpid())
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        logger.info("profile_lock released %s pid=%d", lock_path, os.getpid())


def kill_stale_profile_chrome():
    # 清残留 Testing chrome (同 user-data-dir=douyin_profile) 释放 profile 独占锁.
    # 审计 0908 F1: 须已持 profile_lock 后调用 —— 此时任何用 DOUYIN_PROFILE 的 chrome 要么是本进程
    # 自己的 (刚 launch 或前次 ctx 残留), 要么是崩溃前任进程的孤儿 (进程死锁未释放). age<STALE_SECONDS
    # 跳过 (可能本进程活跃 ctx); age>=STALE_SECONDS 或 lstart parse 失败视为孤儿 kill.
    # 审计 P1-15: 旧 SIGTERM 所有带 profile chrome 误杀并发合法任务, age 判定只杀陈旧.
    # 审计 0906 P3-4: 裸 int() env 非数字 -> ValueError -> 工具不可用, try/except 回退 120.
    try:
        STALE_SECONDS = int(os.environ.get("DOUYIN_CHROME_STALE_SEC", "120"))
    except ValueError:
        logger.warning("invalid DOUYIN_CHROME_STALE_SEC=%r, fallback 120", os.environ.get("DOUYIN_CHROME_STALE_SEC"))
        STALE_SECONDS = 120
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "Google Chrome for Testing"],
            stderr=subprocess.DEVNULL,
        )
        pids = [int(x) for x in out.split() if x]
    except subprocess.CalledProcessError:
        return 0
    killed = 0
    for pid in pids:
        try:
            cmd = subprocess.check_output(
                ["ps", "-o", "command=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "ignore")
        except subprocess.CalledProcessError:
            continue
        if DOUYIN_PROFILE not in cmd:
            continue
        age = 0
        parse_ok = True
        try:
            lstart = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "ignore").strip()
            dt = _dt.datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y")
            age = (_dt.datetime.now() - dt).total_seconds()
        except (subprocess.CalledProcessError, ValueError):
            # 审计 0906 P3-8: parse 失败与年轻进程混 age=0 -> stale chrome 永不清. 标 age=-1 视为孤儿 kill.
            parse_ok = False
            age = -1
            logger.warning("lstart parse fail pid=%d, treat as stale-or-unknown -> kill", pid)
        if parse_ok and 0 < age < STALE_SECONDS:
            logger.info("skip young profile chrome pid=%d age=%ds (likely active session)", pid, int(age))
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
            logger.warning("killed stale profile chrome pid=%d age=%ds", pid, int(age))
            # 审计 P1-16: 旧 os.waitid(os.P_ALL,0) 抢收任意子进程 -> 偷 ffmpeg/httpx 退出状态. 改 P_PID.
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG)
            except ChildProcessError:
                pass
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("no permission to kill chrome pid=%d", pid)
    return killed


async def launch_with_retry(p, headless, retries=1):
    # launch_persistent_context 失败 (TargetClosedError/profile 占用) 时清残留 chrome 后重试一次.
    # 审计 0908 F1: 调用方须已持 profile_lock —— 并发 Playwright 工具经 flock 串行, 无互杀.
    # stats/fetch/reply 旧实现裸 launch 无恢复, 碰撞即失败零恢复 —— 统一走此路径补齐 (F1 修正④).
    last_err = None
    for attempt in range(retries + 1):
        kill_stale_profile_chrome()
        try:
            ctx = await p.chromium.launch_persistent_context(
                DOUYIN_PROFILE, headless=headless
            )
            if attempt > 0:
                logger.info("launch succeeded on retry %d", attempt)
            return ctx
        except Exception as e:
            last_err = e
            msg = str(e)
            if attempt < retries and ("TargetClosedError" in type(e).__name__ or "closed" in msg.lower()):
                logger.warning("launch failed (attempt %d): %s, retry after cleanup", attempt + 1, msg[:80])
                await asyncio.sleep(2)
                continue
            raise
    raise last_err


def parse_bool(val, default=False):
    # 统一布尔解析 (原 publish/self_comment 各一份逐字副本, 审计 A4 重复代码温床).
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "y", "on"):
        return True
    if s in ("false", "0", "no", "n", "off", ""):
        return False
    return default
