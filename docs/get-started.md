---
doc_id: fusion-operation/get-started
product: fusion-operation
class: app
entry: service
doc_type: get-started
title: { zh: "fusion-operation 快速上手", en: "fusion-operation quick start" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - run_graph.py
  - web_api.py
---

# 快速上手 / quick start

前置条件 / prerequisites: Python 3.12（fusion 共享 venv）、Playwright chromium、ffmpeg、fusion-mlx 本地推理（localhost:11434 或经 gateway）、fusion-agent-studio daemon（graph 执行优先走其 RPC）。

## 1. 安装 / install

```bash
cd ~/fusion/fusion-operation
source ~/fusion/.venv/bin/activate
pip install -e ".[test]"
python -m playwright install chromium
```

## 2. 先跑 dry_run 验证链路 / verify the pipeline with dry_run

```bash
python run_graph.py graphs/douyin_auto_publish.json true   # true = dry_run
```

dry_run 全链路真实执行（造片→浏览器走位）但停在发布前，不产生真实作品。首次运行会验证 MLX、Playwright、ffmpeg 三个依赖。
A dry_run executes the full pipeline (production + browser steps) but stops before publishing, creating no real post. The first run validates the MLX, Playwright and ffmpeg dependencies.

单元测试基线 / unit-test baseline:

```bash
pytest tests/
```

## 3. 启动 REST 服务 / start the REST service

```bash
python web_api.py                  # 默认 127.0.0.1:11431
curl http://127.0.0.1:11431/api/health
```

服务为薄层：graph 执行委托 agent-studio daemon RPC（daemon 不在则本地回落），与 cron、CLI 同路。
The service is a thin layer: graph execution is delegated to the agent-studio daemon RPC (local fallback when the daemon is down), the same path cron and the CLI use.

## 4. 触发一次运行 / trigger a run

```bash
curl -s -X POST http://127.0.0.1:11431/api/run \
  -H "Authorization: Bearer $MLX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"graph":"douyin_auto_publish","dry_run":true}'
# -> {"run_id": "..."}  立即返回，长图 5-90 分钟
curl http://127.0.0.1:11431/api/runs/{run_id}   # 轮询状态
```

## 5. 接入定时运营（可选）/ scheduled operations (optional)

```bash
python sync_graphs.py              # 注册 graphs 进 store（graph_id 每次会变）
```

节奏参考（当前 cron.db 配置）：发布 4 次/天（09:05/11:05/14:05/18:05，高峰窗口 `douyin_config.json` 的 `scheduler.peak_windows`）、评论回复 2 次/天（13:35/21:35）、evolve 1 次/天（23:15）。daemon 与 fusion-mlx 均有 launchd KeepAlive。
Reference cadence (current cron.db): publishing 4x/day (09:05 / 11:05 / 14:05 / 18:05, peak windows from `scheduler.peak_windows` in douyin_config.json), comment replies 2x/day (13:35 / 21:35), evolve 1x/day (23:15). Both the daemon and fusion-mlx run under launchd KeepAlive.

## 6. 停止 / stop

`python web_api.py` 为前台进程，`Ctrl-C` 停止；cron 路径不依赖该服务（daemon 独立运行）。
`python web_api.py` is a foreground process; stop it with `Ctrl-C`. The cron path does not depend on this service (the daemon runs independently).

## 约束 / limits

- 认证：bearer token 为配置的 MLX client key；未配置 key（匿名开发）时写接口开放但记警告，读接口始终开放；默认只绑 127.0.0.1。
  Auth: the bearer token is the configured MLX client key; with no key configured (anonymous dev) writes stay open with a logged warning and reads are always open; binds 127.0.0.1 by default.
- 运行注册表在内存中：服务重启后历史 run_id 失效。
  The run registry is in-memory: historical run_ids are lost on service restart.
- 真实发布需要浏览器登录态与 `dry_run=false`；涉及真实账号操作，先跑 dry_run 验证。
  Real publishing needs a logged-in browser session and `dry_run=false`; this operates on a real account, so always verify with a dry_run first.

更多内容见 / see also: docs/api-reference.md, docs/config-reference.md, docs/troubleshooting.md
