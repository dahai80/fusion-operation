---
doc_id: fusion-operation/product-page
product: fusion-operation
class: app
entry: service
doc_type: product-page
title: { zh: "fusion-operation 产品页", en: "fusion-operation product page" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - web_api.py
  - run_graph.py
---

# fusion-operation 产品页 / product page

## 一句话定位 / positioning

抖音增长运营的 DAG 编排服务：12 个生产工具（脚本/配图/配音/合成/发布/评论/回复/统计/报表/队列/选题）挂到 fusion-agent-studio AgentRuntime 上，以图定义发布、评论回复、批量造片等流水线，经 REST API（`web_api.py` :11431）、CLI（`run_graph.py`）与 daemon cron 三条同构路径驱动。
A DAG orchestration service for Douyin growth operations: 12 production tools (script / image / TTS / compose / publish / comments / reply / stats / report / queue / topics) are registered onto the fusion-agent-studio AgentRuntime, with graphs for publishing, comment-reply and batch production, driven through three equivalent paths — the REST API (`web_api.py` :11431), the CLI (`run_graph.py`) and daemon cron.

## 它替用户免掉了什么 / what users no longer have to do

- 不再手工剪竖版短视频：`mlx_script → mlx_image → mlx_tts → ffmpeg_compose` 自动产出 1080×1920 mp4（Ken Burns 效果）。
  No more hand-editing vertical videos: `mlx_script -> mlx_image -> mlx_tts -> ffmpeg_compose` produce a 1080x1920 mp4 automatically (Ken Burns effect).
- 不再手工盯评论回评：`douyin_fetch_comments → mlx_reply_draft → douyin_reply` 幂等批量回复（`out/ops/replied.json` 防重）。
  No more manual comment replies: `douyin_fetch_comments -> mlx_reply_draft -> douyin_reply` replies in batch, idempotently (`out/ops/replied.json` de-dup).
- 不再手工排发布档期：queue_ops 库存队列 + 高峰窗口门控 + 8 个 cron job 按节奏自动发布。
  No manual publish scheduling: the queue_ops inventory plus peak-window gating and 8 cron jobs publish on a fixed cadence.
- 不再手工做运营复盘：`ops_report` 产出全貌/排行/归因报表，`perf_analyze` 把爆款共性回灌到选题与脚本约束。
  No manual ops retrospectives: `ops_report` produces overview / ranking / attribution reports, and `perf_analyze` feeds winning patterns back into topic and script constraints.

## 约束与边界 / constraints and limits

- 真实发布依赖 Playwright 驱动本机浏览器登录态；`dry_run=true` 停在发布前，不产生真实作品。
  Real publishing relies on a Playwright-driven local browser session; `dry_run=true` stops before publishing and creates no real post.
- graph 执行优先走 agent-studio daemon RPC，daemon 不可用回落本地执行；长图 5-90 分钟，REST 只返回 run_id 异步轮询。
  Graph execution prefers the agent-studio daemon RPC and falls back to local execution; long graphs take 5-90 minutes, so the REST API returns a run_id for async polling.
- 依赖 fusion-mlx（`MLX_BASE`，默认本地推理）；模型不可用时脚本/配音/回复草拟降级或失败。
  Depends on fusion-mlx (`MLX_BASE`, local inference); script / TTS / reply drafting degrade or fail when the model is unavailable.
- 播放时长/完播率抖音未开放，互动率以（点赞+评论+分享）/播放 代理。
  Watch-time and completion-rate metrics are not exposed by Douyin; engagement rate (likes+comments+shares)/plays is used as a proxy.

## 入口 / entry points

| 入口 / entry | 说明 / description |
|---|---|
| REST API | `http://127.0.0.1:11431`（/api/health /queue /scheduler /graphs /run /runs） |
| CLI | `python run_graph.py graphs/douyin_auto_publish.json true`（true=dry_run） |
| cron | agent-studio daemon cron.db：发布 4 次/天 + 评论回复 2 次/天 + evolve 1 次/天 |
| 图定义 | graphs/*.json（sync_graphs.py 注册进 store） |

## 用户与场景 / audience and scenarios

- 运营人员：配置 douyin_config.json 与 graphs 后由 cron 无人值守运转，用 ops_report 复盘。
  Operators: after configuring douyin_config.json and the graphs, cron runs unattended and ops_report provides retrospectives.
- 工程师：经 REST API 触发/轮询 graph 运行，或用 run_graph.py 单跑某张图做验证。
  Engineers: trigger and poll graph runs over the REST API, or run a single graph with run_graph.py for verification.
