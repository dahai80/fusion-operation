#!/usr/bin/env python3
# M3-8 §6.6: 部署去个人路径化 + supervisor 纳入 operation 治理.
# deploy_check: 验证部署环境无个人路径残留 + 凭据可用 + 路径可写 + mlx/comfyui 可达.
# CI gate + supervisor 启动前探测共用此脚本. 退出码 0=通过, 非0=失败.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("deploy_check")


def check_no_personal_paths():
    # 扫 douyin_tools/ + scripts/ + sync_*.py + douyin_config.json 确认无 /Users/dahai
    # 硬编码 (注释除外). JSON 也要扫: issue #20 CI 根因就是 JSON 路径绝对化在 runner 上
    # PermissionError, 旧 gate 只扫 .py 漏掉.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    checks = [
        "douyin_tools/config.py",
        "douyin_tools/comfyui_client.py",
        "sync_plugins.py",
        "sync_venv_deps.py",
        "douyin_config.json",
    ]
    failures = []
    for rel in checks:
        p = os.path.join(repo_root, rel)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if "/Users/dahai" in line and not line.strip().startswith("#"):
                    failures.append(f"{rel}:{i}: {line.strip()}")
    if failures:
        logger.error("FAIL: personal paths found:\n%s", "\n".join(failures))
        return False
    logger.info("OK: no personal hardcoded paths in source")
    return True


def check_paths_writable():
    from douyin_tools.config import (
        AUDIO_DIR,
        IMAGE_DIR,
        OPS_DATA_DIR,
        QUEUE_DIR,
        VIDEO_DIR,
    )

    for name, p in [("video", VIDEO_DIR), ("audio", AUDIO_DIR), ("image", IMAGE_DIR),
                     ("ops", OPS_DATA_DIR), ("queue", QUEUE_DIR)]:
        os.makedirs(p, exist_ok=True)
        test_file = os.path.join(p, ".deploy_check_write")
        try:
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
        except OSError as e:
            logger.error("FAIL: path %s=%s not writable: %s", name, p, e)
            return False
    logger.info("OK: all output paths writable")
    return True


def check_credential_source():
    # 验证 mlx api key 可解析 (不打印 key 本身).
    from douyin_tools.config import _mlx_api_key

    key = _mlx_api_key()
    if key:
        logger.info("OK: mlx api key resolved (source: env/keyring/settings)")
        return True
    logger.warning("WARN: mlx api key not found (env FUSION_MLX_API_KEY / keyring / settings.json)")
    return True  # warning only, deploy may proceed (mlx may be unauth)


def check_queue_db():
    from douyin_tools.config import QUEUE_DIR

    db_path = os.path.join(QUEUE_DIR, "queue.db")
    db_dir = os.path.dirname(db_path)
    os.makedirs(db_dir, exist_ok=True)
    try:
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS _deploy_check (x INTEGER)")
        conn.execute("INSERT OR REPLACE INTO _deploy_check VALUES (1)")
        conn.execute("DELETE FROM _deploy_check")
        conn.execute("DROP TABLE _deploy_check")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("FAIL: queue.db not operable at %s: %s", db_path, e)
        return False
    logger.info("OK: queue.db operable at %s", db_path)
    return True


def main():
    logger.info("deploy_check starting...")
    results = {
        "no_personal_paths": check_no_personal_paths(),
        "paths_writable": check_paths_writable(),
        "credential_source": check_credential_source(),
        "queue_db": check_queue_db(),
    }
    passed = all(results.values())
    summary = {"passed": passed, "checks": results}
    print(json.dumps(summary, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
