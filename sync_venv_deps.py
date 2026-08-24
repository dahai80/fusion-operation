import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# plugin 运行依赖: fusion-operation 的 douyin_tools 装在 monorepo 共享 .venv,
# agent-studio daemon 用自己独立 .venv 加载 plugin shim. 独立 venv 不含 cv2/playwright
# 等 douyin_tools 依赖 -> 12 个 plugin 加载失败 (No module named 'cv2') -> publish_scheduler
# 缺失 -> 高峰发布 cron 命中 gate_peak=false -> 实际 0 发布 (2026-08-18 事故根因).
# 修复: 从共享 venv site-packages 把缺失依赖拷进 agent-studio venv, 幂等可重跑.
SHARED_VENV = Path(os.environ.get("FUSION_SHARED_VENV", "/Users/dahai/fusion/.venv"))
AGENT_VENV = Path(
    os.environ.get(
        "FUSION_AGENT_STUDIO_VENV",
        "/Users/dahai/fusion/fusion-agent-studio/.venv",
    )
)

# 必须与共享 venv 对齐的依赖包 (目录名 + dist-info 名).
REQUIRED_PACKAGES = [
    "cv2",
    "opencv_python-4.13.0.92.dist-info",
    "playwright",
    "playwright-1.62.0.dist-info",
    "greenlet",
    "greenlet-3.5.4.dist-info",
    "pyee",
    "pyee-13.0.1.dist-info",
]


def _shared_site_packages():
    shared_py = SHARED_VENV / "bin" / "python3"
    if not shared_py.exists():
        logger.warning("shared venv python not found: %s", shared_py)
        return None
    # 共享 venv 与当前进程同 py 版本, 直接定位 site-packages 目录.
    for p in (SHARED_VENV / "lib").glob("python*/site-packages"):
        if (p / "cv2").exists():
            return p
    logger.warning("cv2 not in shared venv site-packages: %s", SHARED_VENV)
    return None


def _agent_site_packages():
    agent_py = AGENT_VENV / "bin" / "python3"
    if not agent_py.exists():
        logger.warning("agent-studio venv python not found: %s", agent_py)
        return None
    for p in (AGENT_VENV / "lib").glob("python*/site-packages"):
        return p
    return None


def sync():
    shared_sp = _shared_site_packages()
    agent_sp = _agent_site_packages()
    if not shared_sp or not agent_sp:
        logger.error("cannot locate site-packages: shared=%s agent=%s", shared_sp, agent_sp)
        print("sync_venv_deps: FAIL, cannot locate site-packages")
        return 1

    logger.info("shared site-packages: %s", shared_sp)
    logger.info("agent site-packages: %s", agent_sp)

    copied = []
    skipped = []
    missing = []
    for pkg in REQUIRED_PACKAGES:
        src = shared_sp / pkg
        dst = agent_sp / pkg
        if not src.exists():
            missing.append(pkg)
            logger.warning("shared venv missing package: %s", pkg)
            continue
        if dst.exists():
            skipped.append(pkg)
            logger.info("already present in agent venv: %s", pkg)
            continue
        shutil.copytree(src, dst, symlinks=True)
        copied.append(pkg)
        logger.info("copied %s -> %s", src, dst)

    print(
        f"sync_venv_deps: shared={SHARED_VENV} agent={AGENT_VENV} "
        f"copied={copied} skipped={skipped} missing_in_shared={missing}"
    )
    if missing:
        logger.warning("some packages missing in shared venv, install them in shared venv first: %s", missing)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(sync())
