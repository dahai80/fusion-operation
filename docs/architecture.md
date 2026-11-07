---
doc_id: fusion-operation/architecture
product: fusion-operation
class: app
entry: service
doc_type: architecture
title: { zh: "fusion-operation 架构", en: "fusion-operation architecture" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - web_api.py
  - run_graph.py
---

# 架构 / architecture

## 分层 / layers

```
fusion-mlx (localhost:11434)          ── 多模态推理：脚本/配图/配音
   ▼
douyin_tools/ (12 个 BaseTool)         ── mlx_script / mlx_image / mlx_tts / ffmpeg_compose /
                                          douyin_publish / douyin_fetch_comments /
                                          mlx_reply_draft / douyin_reply / douyin_stats /
                                          ops_report / queue_ops / topic_gen
   ▼
fusion-agent-studio AgentRuntime       ── DAG 执行引擎（daemon RPC + cron 调度）
   ▼
graphs/*.json                          ── douyin_auto_publish / douyin_comment_reply /
                                          douyin_batch_produce / douyin_queue_publish / douyin_evolve
   ▲
三条同构触发路径：cron（daemon cron.db） / REST（web_api.py :11431） / CLI（run_graph.py）
```

## 关键决策 / key decisions

- **薄 REST 层**：`web_api.py` 无业务逻辑，graph 执行委托 `run_graph.run_once`——与 cron、CLI 完全同路，三条路径行为一致。
  Thin REST layer: `web_api.py` holds no business logic; graph execution delegates to `run_graph.run_once`, the exact same path cron and the CLI take, keeping all three consistent.
- **异步运行注册表**：长图 5-90 分钟，`POST /api/run` 起后台 asyncio 任务立即返回 run_id，HTTP 永不阻塞。
  Async run registry: long graphs take 5-90 minutes, so `POST /api/run` starts a background asyncio task and returns a run_id immediately; HTTP never blocks.
- **发布门控**：统一发布图内置审批门 + `gate_peak` 高峰窗口门控（`douyin_config.json` 声明式配置），有库存发布、无库存自动补货后归档。
  Publishing gates: the unified publish graph embeds an approval gate plus the `gate_peak` peak-window gate (declarative config in douyin_config.json), publishing from inventory or auto-restocking first.
- **幂等与回灌**：`douyin_reply` 以 `out/ops/replied.json` 幂等防重；`perf_analyze` 把真实归因（winning_patterns.json）回灌 `topic_gen` hint 与 `mlx_script` 约束，形成"发布→数据→归因→选题"循环。
  Idempotency and feedback: `douyin_reply` de-dups via `out/ops/replied.json`; `perf_analyze` feeds real attribution (winning_patterns.json) back into `topic_gen` hints and `mlx_script` constraints, closing the publish -> data -> attribution -> topics loop.
- **降级链**：文生图失败回退静态图、kokoro 不可用回退 edge-tts、daemon 不可用回落本地执行——每级降级都记录来源。
  Fallback chains: image generation falls back to static images, kokoro to edge-tts, daemon to local execution — each level logs its source.
- **launchd 守护**：daemon 与 fusion-mlx 双 launchd KeepAlive，cron 节奏不因进程崩溃中断。
  launchd supervision: both the daemon and fusion-mlx run under launchd KeepAlive so the cron cadence survives crashes.

## 数据流 / data flow

```
cron/REST/CLI ──▶ run_graph.run_once ──▶ daemon RPC（或本地执行）
   graph 节点 ──▶ douyin_tools 工具 ──▶ MLX / Playwright / ffmpeg
   产物 ──▶ out/（视频/报表/stats_history.jsonl/winning_patterns.json/replied.json）
   库存 ──▶ queue_ops（enqueue/dequeue/archive）
```

## 边界与限制 / boundaries and limits

- 运行注册表仅在 web_api.py 进程内存中，服务重启丢失；持久执行记录在 daemon cron.db。
  The run registry lives only in the web_api.py process; persistent execution records are in the daemon cron.db.
- 播放时长/完播率抖音 API 未开放，报表用互动率代理。
  Watch-time and completion-rate are not exposed by the Douyin API; reports use engagement rate as a proxy.
- 平台风控不受本项目控制：高频真发/回复可能被限流，节奏由 cron 窗口约束。
  Platform-side risk control is outside this project's control: high-frequency real publishing or replying can be rate-limited; the cron windows bound the cadence.
