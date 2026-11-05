# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

`fusion-operation` 是抖音吸粉自动化编排的落地实现（业务层，调 fusion-mlx 推理 + fusion-comfyui 造片 + fusion-agent-studio DAG/cron 运行时），已进入团队化生产阶段（M3：SQLite 队列、团队契约、熔断重试、keychain 凭据、CI 门禁、团队模板生成）。设计方案见 `~/video/douyin-xifen-operation.md`。

历史验证记录（单次手动 ✅ / cron 持续 ✅ 区分）见 `docs/verification-log.md`，本文件只保留当前事实。

### 构建/测试/运行

```bash
cd /Users/dahai/fusion && source .venv/bin/activate && cd fusion-operation
pip install -e ".[test]"
python -m playwright install chromium       # 首次
pytest tests/ -q                             # 263 用例
pytest tests/test_douyin_tools.py::test_publish_dry_run_flow -v    # 单测
unset FUSION_MLX_API_KEY
python run_graph.py graphs/douyin_auto_publish.json true   # 单条 dry_run 验证（统一发布 Graph）
python run_graph.py graphs/douyin_batch_produce.json true --loop 7 --series 宇宙奥秘  # 批量造片
python run_graph.py graphs/douyin_queue_publish.json true --loop 2                   # 库存发布
./deploy.sh                                  # 统一部署（sync 全链 → daemon 重启 → 校验，任一步失败 exit 1）
```

CI：`.github/workflows/ci.yml`（macos-14，push/PR master 跑 pytest + deploy_check）。单仓 CI 无 monorepo 共享 venv，workflow 内浅克隆 `fusion-core` + `fusion-harness` + `fusion-agent-studio` 安装（`douyin_tools` 依赖 agent-studio 的 `tools.base`/`tools.registry`；config 单点 key 解析依赖 fusion-core，issue #22）。

### 架构

- `douyin_tools/`：22 个 BaseTool（`__init__.py` **自动扫描注册**——新增工具只需建模块定义 BaseTool 子类，无需改注册表；`config`/`script_helpers`/`comfyui_client` 是公共库非工具）：
  - 发布链：`mlx_script`（hook_variant A/B/C + winning_hooks 注入 + 禁人物 + narration_segment 音画同步）/ `mlx_image` / `mlx_tts` / `ffmpeg_compose`（1080×1920 + BGM + 封面标题 overlay + 星球占位兜底非黑首帧）/ `douyin_publish`（Playwright 异步 + 首帧封面 + vid 捕获 + profile 锁硬化）/ `comfyui_image`（竖屏 + 禁人物 negative_prompt）/ `comfyui_tts`（Qwen3-TTS http backend 中文）/ `web_image_search`（NASA 真实天体图，公共领域商用合规，真实图优先）。
  - 评论链：`douyin_fetch_comments` / `mlx_reply_draft`（AUTO_REPLIES 7 规则+安全过滤）/ `douyin_reply`（幂等 `out/ops/replied.json`）。
  - 统计链：`douyin_stats` / `ops_report`（三段式：全貌+细节排行+归因）。
  - 库存链：`queue_ops`（enqueue/dequeue/archive/count，SQLite 后端）/ `topic_gen`（读 winning_patterns 回灌 + 排除已发布/黑名单）。
  - 进化链：`perf_track`（snapshot/rank/by_variant）/ `perf_analyze`（模型归因→pending_patterns.json 待评审）/ `pattern_review`（promote/reject 评审门）/ `self_repair`（scan/retitle/blacklist）/ `publish_scheduler`（高峰窗口判定）。
  - 团队/运维链：`ops_health`（每日巡检：cron 执行实证+队列告警+文件 mtime 停摆检测+vid 回填率）/ `team_contract`（契约读取/校验/周计划 approve）。
- `script_helpers.py`：扁平插值切片抽取；`parse_script` 兼容 dict repr（`ast.literal_eval` 回退）与 JSON 双路径。
- `graphs/`：7 个运营 DAG（auto_publish 统一发布+无库存自动补货回环 / comment_reply(+winner) / batch_produce / queue_publish / evolve / repair_publish）+ 5 个补货 step mini-graph（`douyin_step_*.json`，M3-2 拆解用）。
- `restock_chain.py`：补货 5 步 DAG 声明（script→image/tts→compose→enqueue），单环节失败只重试该环节。
- `run_graph.py`：DAG runner，RPC 优先（agent-studio daemon）本地回退，`--loop N` 批量/日上限。
- `web_api.py`（issue #24）：REST surface（11431，port-registry: fusion-operation）——health/queue/scheduler/graphs + POST /run 异步执行（后台 task + 内存 run 注册表），graph 执行委托 run_graph 同路。写端点 Bearer = client key；daemon RPC 需 `_auth` JWT（#326），`douyin_tools/rpc_auth.py` 用静态 module key 换 JWT（env `FUSION_AGENT_STUDIO_API_KEY` > `~/.fusion-agent-studio/web_module_key`，注意 `~/.fusion-agent-studio/env.local` 同名变量覆盖 start.sh export，两处不一致 → `invalid apikey: key_not_found`）。UI 控制面板留 fusion-web-shell 模块化阶段。

### M3 生产化层（2026-10 落地，commit m3-5..m3-9）

- **SQLite 队列**（`queue_store.py`）：`out/queue/queue.db` 为权威存储（WAL + RLock），文件目录 `pending/inflight/published/failed/` 降为投影（消费者仍读 JSON）。dequeue 带 lease（`LEASE_TTL` 默认 1800s）+ 过期 reclaim + stale inflight 检测。
- **数据门禁**（`history_store.py`）：estimated 播放数据物理隔离到独立文件，永不与真实数据混表（`stats_real`/`stats_estimated` 分流）；`perf_analyze` 只读真实数据。pending_patterns 超 48h 未评审触发 ops_health 告警（`data_gate.pending_stale_hours` 可配）。
- **团队契约**（`team-contract.yaml` + `team_contract.py` + `roles.py`）：文档即契约，7 角色（lead/researcher/writer/imager/voicer/publisher/analyst）× 工具白名单 + 资源独占（browser 1、gpu 1）+ SLA + 幂等（publish_key=slot+video_hash，有 published 证据永不重发）+ 干预点（周计划审批/爆款模式评审/连续 2 次需修复升级人工）。`tests/test_tool_roles.py` 断言契约与 `roles.py` 一致——改角色工具映射两处必须同步。
- **统一重试+熔断**（`resilient_http.py`）：所有 mlx/comfyui HTTP POST 走 `post_json`（3 次指数退避 + 熔断：连续 5 次失败挂起 60s + `team.alert`），不再裸 `client.post`。
- **keychain 凭据**（`credential_store.py`）：MLX API key 优先 macOS Keychain（keyring），回退 env > settings.json；headless/CI keyring 不可用静默降级。
- **团队模板生成**（`scripts/team_init.py` + `templates/douyin-ops/`）：`fusion team init <vertical> --template douyin-ops` 生成契约+角色+graph+数据目录+cron 触发器。

### 部署

`./deploy.sh` 顺序：`sync_venv_deps.py`（cv2/playwright 进 agent-studio venv）→ `sync_plugins.py`（plugin shim 注入 daemon）→ `sync_agent_config.py`（douyin_config.json → definition.json 工具配置注入）→ `sync_graphs.py`（graph 注册进 daemon store + **cron graph_id 回填**）→ daemon 重启 → RPC 校验（tool.list ≥ 20 + failed_plugins 空 + cron ≥ 7 job），失败即 abort。

改 `graphs/*.json` 或 `douyin_config.json` 后需重跑 `deploy.sh`（或单独 `python sync_graphs.py` / `python sync_agent_config.py`）才进生产 daemon。graph 重建后 graph_id 会变，cron job 的 graph_id 靠 `sync_graphs.py` 的回填逻辑维持绑定——**不要手工 `graph.delete` 运营 graph**。

cron 节奏（daemon cron.db，8 job）：4 发布（09:05/11:05/14:05/18:05 `douyin_auto_publish`）+ 2 评论（13:35/21:35）+ 1 进化（23:15 evolve）+ 1 修复（23:45 repair_publish）。cron 触发时区取系统本地时区（上游 #270 修复，曾是 9 月初连续 0 发布根因）。

### 关键约束

- Playwright **必须用 async API**（`async_playwright`）：AgentRuntime 在 asyncio 循环内调用工具，sync API 会报错。
- **扁平插值**：AgentRuntime interpolate 只支持整段 `{{var}}`，不支持 JSON 路径。工具接收整段 `{{script_json}}`，由 `script_helpers` 抽切片；列表经 output_mapping 后以 Python repr 插值，`_parse_list` 系用 `ast.literal_eval` 回退。
- **字符串布尔**：ConditionEngine 是字符串比较，工具输出的布尔字段必须是 `"true"`/`"false"`（同 `has_stock`/`in_peak`/`enough` 模式）。
- **鉴权**：`config.mlx_headers()` 带 `X-Fusion-Route: fusion-operation`（mlx v0.7.0+ 缺失即 403）；key 解析走 fusion_core.config 单点（issue #22）：env（`FUSION_CLIENT_KEY` > `FUSION_MLX_API_KEY` > `MLX_API_KEY`）> keychain > settings.json，`FUSION_PROFILE=prod` 全链空才 RuntimeError。shell 里 stale `FUSION_MLX_API_KEY` 会导致 401，本地跑 runner 需 `unset`；daemon/comfyui 的 start.sh 启动时已统一 unset。经 gateway 与直连 mlx 是两套 key，不可混用。
- 抖音登录态：持久化 profile `~/video/douyin_profile`；同 profile Chrome 独占锁会 TargetClosedError，`douyin_publish` 有自清残留重试。
- **声明式配置**：业务参数集中在 `douyin_config.json`，`config.py` 三级回退（env > JSON > 硬编码默认），带 mtime 失效缓存（daemon 长进程改 JSON 不重启即生效）。自定义路径 `DOUYIN_CONFIG_FILE`。`paths.*` 必须相对仓根或 `~/` 前缀（`_resolve_path` 统一解析，禁绝对个人路径——CI runner 无本地 home，`deploy_check.py` + 回归测试双门禁拦截）。
- 产物全部落 `out/`（.gitignore）；运营数据 `out/ops/`，队列 `out/queue/`（SQLite 权威 + 目录投影）。
- 上游问题不改上游仓库：先提 issue，再 PR，等合并；下游保持回退不阻塞生产（历史先例见 `docs/verification-log.md`）。

### 验证状态（当前快照）

- 254 单测全绿；CI 门禁在 master push/PR 生效（2026-10-26 起 master 绿，issue #20/#21/#22 已闭）。
- 生产 daemon launchd KeepAlive（mlx/daemon/comfyui 三服务均 launchd 持久化），56 tools 0 failed_plugins，8 cron job 绑定正确。
- **两类验证不可混用**：手动跑通一次 ≠ cron 持续运行。判断自动化是否真在跑用 `ops_health` 工具每日巡检 + `cron.list_executions` 实证 + 关键文件 mtime。
- 详细历史验证记录与根因分析：`docs/verification-log.md`。

## Monorepo Context

This project lives inside the Fusion monorepo. The parent `/Users/dahai/fusion/CLAUDE.md` is the source of truth for cross-project facts:

- Shared `.venv/` at the repo root — `source .venv/bin/activate` before any Python work
- `fusion-mlx` inference engine serves `localhost:11434`; manage with `fusion-mlx/start.sh start|stop|status|log|doctor`
- Model downloads via mirror `https://hf-mirror.com`; cache at `~/.fusion-mlx/models`
- Python sub-projects share patterns: `pip install -e ".[test]"`, `pytest tests/` (`asyncio_mode=auto`), setuptools backend with `[tool.setuptools.packages.find]`, Python ≥3.11
- Code style: 4-space-multiple indentation, no docstrings, always include logging

## Constraints (inherited from global + repo instructions)

- Only modify code within this project directory and the monorepo; upstream problems → file issue, then PR, then land code (do not patch other projects directly)
- Read adjacent files before writing new ones; match existing monorepo patterns
- For LLM-related tests, load the real model via the `fusion-mlx` lifecycle commands above
- On task completion, update `README.md` if needed
