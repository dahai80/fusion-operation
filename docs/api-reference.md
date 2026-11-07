---
doc_id: fusion-operation/api-reference
product: fusion-operation
class: app
entry: service
doc_type: api-reference
title: { zh: "fusion-operation REST API 参考", en: "fusion-operation REST API reference" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - web_api.py
---

# REST API 参考 / REST API reference

Base URL: `http://127.0.0.1:11431`（`FUSION_OPERATION_API_PORT` 可改）。服务为 issue #24 定义的薄层，无业务逻辑：graph 执行委托 `run_graph.run_once`（agent-studio daemon RPC 优先，本地回落），队列/调度/健康读 queue_store 与 daemon RPC。

## 认证 / auth

- Bearer token = 配置的 MLX client key（`_mlx_api_key()` 解析链）。
  The bearer token equals the configured MLX client key (resolved via `_mlx_api_key()`).
- 未配置 key 时写接口开放但记警告；读接口始终开放。无业务端点豁免中间件（服务本身就是回环绑定）。
  With no key configured, writes stay open with a logged warning; reads are always open. No exempt-path list exists (the service is loopback-bound by design).
- 错误响应：`{ detail: ... }`（HTTPException 处理器统一）。
  Error responses: `{ detail: ... }` (unified by the HTTPException handler).

## 端点 / endpoints

| 方法 / method | 路径 / path | 说明 / description |
|---|---|---|
| GET | `/api/health` | MLX 健康 + daemon 可达性（agent-studio socket） |
| GET | `/api/queue` | 视频库存队列状态（queue_store） |
| GET | `/api/scheduler` | daemon cron job 列表（注入 `_auth` JWT） |
| GET | `/api/graphs` | 本地 `graphs/*.json` 文件清单 |
| POST | `/api/run` | 启动 graph 运行：`{"graph":"douyin_auto_publish","dry_run":true}`；后台 asyncio 任务，立即返回 `{"run_id": ...}`，绝不阻塞 HTTP（长图 5-90 分钟） |
| GET | `/api/runs` | 当前进程内运行注册表 |
| GET | `/api/runs/{run_id}` | 单次运行状态轮询 |

## 运行语义 / run semantics

- 运行注册表在内存中：`POST /api/run` 的 run_id 仅在当前进程有效，服务重启即失效；持久化执行记录看 daemon `cron.list_executions`。
  The run registry is in-memory: run_ids from `POST /api/run` are valid only for the current process; persistent execution history lives in the daemon's `cron.list_executions`.
- graph 参数与 dry_run 语义同 CLI：`dry_run=true` 停在发布前。
  Graph arguments and dry_run semantics match the CLI: `dry_run=true` stops before publishing.
- daemon 不可达时本地回落执行，结果一致但无 daemon 侧持久化。
  When the daemon is unreachable, execution falls back to local with identical results but no daemon-side persistence.

## CLI / cron 等价路径 / CLI and cron equivalents

```bash
python run_graph.py graphs/douyin_auto_publish.json true        # dry_run 单次
python run_graph.py graphs/douyin_batch_produce.json true --loop 7 --series 宇宙
python run_graph.py graphs/douyin_queue_publish.json true --loop 2
python verify_graph_b.py                                        # 评论回复链路验证
```

cron 与 REST、CLI 共用 `run_graph.run_once`，行为一致性由图定义保证。
cron, REST and the CLI all funnel through `run_graph.run_once`; consistency is guaranteed by the shared graph definitions.

## 约束 / constraints

- 服务不做鉴权中间件之外的输入校验：graph 名必须是本地 `graphs/` 下存在的文件名。
  Beyond the auth layer the service performs no extra input validation: the graph name must be an existing file under local `graphs/`.
- 单进程同时运行的图数量不受 API 限制，受 daemon/本机资源约束（Playwright + ffmpeg 并发开销大）。
  The API does not cap concurrent graphs; the daemon and local resources do (Playwright and ffmpeg are expensive to run concurrently).
