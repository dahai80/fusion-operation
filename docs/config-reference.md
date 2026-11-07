---
doc_id: fusion-operation/config-reference
product: fusion-operation
class: app
entry: service
doc_type: config-reference
title: { zh: "fusion-operation 配置参考", en: "fusion-operation config reference" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - douyin_tools/config.py
  - douyin_config.json
  - web_api.py
---

# 配置参考 / config reference

配置集中在 `douyin_tools/config.py`（环境变量）与 `douyin_config.json`（声明式业务配置）。
Configuration lives in `douyin_tools/config.py` (environment variables) and `douyin_config.json` (declarative business config).

## 服务 / service

| 变量 / variable | 默认值 / default | 说明 / description |
|---|---|---|
| `FUSION_OPERATION_API_PORT` | `11431` | web_api.py 端口（port-registry 登记 fusion-operation） |
| `--host`（CLI 参数） | `127.0.0.1` | web_api.py 绑定地址，保持回环 |

## 上游 / upstream

| 变量 / variable | 说明 / description |
|---|---|
| `MLX_BASE` | fusion-mlx 推理地址（默认本地）；`/v1/chat/completions` `/v1/images/generate` `/v1/audio/speech` |
| `MLX_TIMEOUT` | MLX 请求超时 |
| `MLX_SETTINGS_PATH` / `MLX_SETTINGS` | MLX 客户端设置与 key 解析 |
| `AGENT_STUDIO_SOCKET` | agent-studio daemon UDS socket（graph RPC + cron 管理，注入 `_auth` JWT） |
| `DOUYIN_CONFIG_FILE` | douyin_config.json 路径覆盖 |

## 目录 / directories

| 变量 / variable | 用途 / purpose |
|---|---|
| `DOUYIN_OPS_DATA_DIR` | 运营数据根目录（报表/归因/统计） |
| `DOUYIN_QUEUE_DIR` | queue_ops 库存队列目录 |
| `DOUYIN_VIDEO_DIR` / `DOUYIN_AUDIO_DIR` / `DOUYIN_IMAGE_DIR` | 造片产物目录 |
| `DOUYIN_TOPICS_FILE` | 选题库（topic_gen 读写） |
| `DOUYIN_FALLBACK_IMAGES` | 文生图失败时的静态图回退 |
| `DOUYIN_LEASE_TTL` | 队列租约 TTL |
| `DOUYIN_PROFILE` / `DOUYIN_HOME_URL` / `DOUYIN_UPLOAD_URL` / `DOUYIN_MANAGE_URL` / `DOUYIN_COMMENT_URL` / `DOUYIN_ITEM_LIST_API` | Playwright 操作的页面地址 |

## douyin_config.json（业务配置）/ declarative business config

- `scheduler.peak_windows`：高峰发布窗口（当前 `["9-10","11-12","14-15","18-19"]`），`publish_scheduler` 动态读取；cron 发布 4 次/天必须与窗口一致，否则 `gate_peak` 门控会拦下非窗口触发。
  `scheduler.peak_windows`: peak publishing windows (currently `["9-10","11-12","14-15","18-19"]`), read dynamically by `publish_scheduler`; the 4x/day cron schedule must stay aligned with the windows or the `gate_peak` gate blocks out-of-window triggers.
- AUTO_REPLIES 7 规则：`mlx_reply_draft` 的回复约束 + 安全过滤。
  The 7 AUTO_REPLIES rules: reply constraints plus safety filtering for `mlx_reply_draft`.

## launchd 持久化 / launchd persistence

- agent-studio daemon 与 fusion-mlx 均配置 launchd KeepAlive（`~/Library/LaunchAgents/`），开机自启、崩溃拉起。
  Both the agent-studio daemon and fusion-mlx run under launchd KeepAlive (`~/Library/LaunchAgents/`): start at boot, restart on crash.
- web_api.py 不由 launchd 托管（按需手动启动）；cron 路径不依赖它。
  web_api.py is not launchd-managed (started manually when needed); the cron path does not depend on it.

## 约束 / constraints

- 凭据（cookie/登录态）由 `credential_store.py` 管理，勿写入文档或提交仓库。
  Credentials (cookies / login state) are managed by `credential_store.py`; never write them into docs or commits.
- 无配置热加载：改环境变量或 douyin_config.json 后需重启对应进程（daemon 重载由其自身机制负责）。
  No hot reload: restart the affected process after changing environment variables or douyin_config.json (the daemon reloads via its own mechanism).
- graph_id 每次 `sync_graphs.py` 重建会变；cron 注册前先 sync 取最新 id。
  graph_ids change on every `sync_graphs.py` rebuild; run sync before (re)registering cron jobs.
