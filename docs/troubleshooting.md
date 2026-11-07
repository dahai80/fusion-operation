---
doc_id: fusion-operation/troubleshooting
product: fusion-operation
class: app
entry: service
doc_type: troubleshooting
title: { zh: "fusion-operation 故障排查", en: "fusion-operation troubleshooting" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - run_graph.py
  - docs/verification-log.md
---

# 故障排查 / troubleshooting

## 症状速查 / symptom index

| 症状 / symptom | 处理 / fix |
|---|---|
| `POST /api/run` 401/403 | 未带或错带 `Authorization: Bearer`；token 必须等于配置的 MLX client key |
| run 一直 pending | 优先查 agent-studio daemon 是否存活（`/api/health`）；daemon 挂了会本地回落，本机资源不足时排队 |
| mlx_script/配图/配音失败 | fusion-mlx 不可达或模型未加载；`curl $MLX_BASE/v1/models` 验证，必要时 `fusion-mlx/start.sh start` |
| Playwright 报浏览器未装 | `python -m playwright install chromium`（首次必装） |
| ffmpeg 合成失败 | 确认 ffmpeg 在 PATH；检查 `DOUYIN_VIDEO_DIR` 磁盘空间 |
| 文生图失败 | 自动回退静态图（`DOUYIN_FALLBACK_IMAGES`），属降级非故障；要真图先确认 MLX 图像端点可用 |
| 评论回复重复回复 | `out/ops/replied.json` 幂等记录被删；恢复该文件或接受重复风险 |
| cron 没触发发布 | `GET /api/scheduler` 看 daemon cron job；注意 `gate_peak` 高峰门控——发布窗口外触发会被拦（对齐 `douyin_config.json` 的 `scheduler.peak_windows`） |
| graph_id 对不上 | 每次 `sync_graphs.py` 重建 graph 后 id 会变；先 sync 再注册/更新 cron |
| 实盘发布没产出 vid | 确认 `dry_run=false` 且浏览器登录态有效；dry_run 按设计停在发布前 |

## 诊断步骤 / diagnosis steps

1. `curl http://127.0.0.1:11431/api/health` — MLX 与 daemon 双检查。
   Checks both MLX and daemon reachability.
2. `GET /api/runs` 与 `GET /api/runs/{run_id}` — 进程内运行状态（重启后失效，持久记录看 daemon）。
   In-process run status (lost on restart; persistent history is in the daemon).
3. `python run_graph.py graphs/douyin_auto_publish.json true` — CLI 复跑 dry_run，绕开 REST 层定位问题在图还是服务。
   Re-run a dry_run via the CLI, bypassing the REST layer, to isolate graph vs service issues.
4. `python verify_graph_b.py` — 评论回复链路专项验证。
   Targeted verification of the comment-reply pipeline.
5. 工具级巡检 `ops_health`（每日 cron 已配置）+ `out/ops/` 下报表与 `stats_history.jsonl`。
   Tool-level inspection via `ops_health` (a daily cron exists) plus reports under `out/ops/` and `stats_history.jsonl`.
6. 测试基线：`pytest tests/`（10 工具 + 35 单测基线，持续扩充）。
   Test baseline: `pytest tests/` (baseline of 10 tools and 35 unit tests, still growing).

## 两类验证不可混用 / two kinds of verification

「单次手动验证」只证明路径跑通过一次；「cron 持续运行」需 `cron.list_executions` 实证 + 关键文件 mtime 持续更新。判断 cron 是否真在跑用 `ops_health` 巡检，不要拿历史手动结论当现状。
A one-shot manual verification only proves the path worked once; "cron keeps running" requires `cron.list_executions` evidence plus continuously updated file mtimes. Use the `ops_health` inspection to judge cron liveness instead of trusting historical manual results.

## 安全与真发 / safety and real publishing

- 真发 = `dry_run=false` + 有效登录态：会真实上传作品、发布评论回复。涉及真实账号，逐图验证后再上 cron。
  Real publishing = `dry_run=false` plus a valid session: it uploads posts and posts comment replies for real. Verify each graph before putting it on cron.
- `douyin_reply` 批量回复有幂等记录与安全过滤（AUTO_REPLIES 规则 + 模型草拟），但平台风控仍可能限流，控制频率。
  `douyin_reply` has idempotency records and safety filtering (AUTO_REPLIES rules plus model drafting), but platform rate limits still apply — keep the frequency bounded.

## 数据与清理 / data and cleanup

- `out/` 为产物目录（gitignored）：视频/音频/图片/报表/幂等记录都在其中；删除 `out/ops/replied.json` 会失去回复幂等。
  `out/` is the artifact directory (gitignored): videos, audio, images, reports and idempotency records live there; deleting `out/ops/replied.json` loses reply idempotency.
- 历史验证与根因记录见 docs/verification-log.md；当前事实以根目录 CLAUDE.md 为准。
  Historical verification and root-cause notes live in docs/verification-log.md; current facts are tracked in the root CLAUDE.md.
