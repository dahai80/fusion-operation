---
doc_id: fusion-operation/readme
product: fusion-operation
class: app
entry: service
doc_type: readme
title: { zh: "fusion-operation 说明", en: "fusion-operation readme" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
---

# fusion-operation

基于 Fusion 生态（fusion-mlx 推理引擎 + fusion-agent-studio DAG 运行时）的抖音吸粉自动化编排落地实现。研读 `~/video` 抖音吸粉逻辑，做成可编排、可生产、可商业发布的 DAG 工作流。

设计方案：`~/video/douyin-xifen-operation.md`

## 架构

```
fusion-mlx (localhost:11434)          ── 多模态推理：脚本/配图/配音
   │ /v1/chat/completions  /v1/images/generate  /v1/audio/speech
   ▼
douyin_tools/                          ── 12 个生产工具（BaseTool）
   mlx_script          生成吸粉脚本 JSON {title,description,narration,scenes[]}
   mlx_image           文生图，失败回退静态图
   mlx_tts             语音合成，kokoro 不可用时回退 edge-tts
   ffmpeg_compose      Ken Burns + ffmpeg 合成 1080×1920 竖版 mp4
   douyin_publish      Playwright 自动上传/填描述/设封面，dry_run 不真发；真发后捕获新作品 vid 返回
   douyin_fetch_comments 抓取作品评论列表（has_new 判定）
   mlx_reply_draft     真实模型草拟回复，注入 AUTO_REPLIES 7 规则 + 安全过滤
   douyin_reply        批量点赞+回复（幂等 out/ops/replied.json）
   douyin_stats        抓取账号粉丝/获赞 + 作品播放/点赞/评论/分享（item/list API + 页面正文滚动解析补齐 play/like/share）
   ops_report          运营统计报表：先全貌(账号+汇总+分布+variant A/B/C对比)→逐视频细节排行(按 plays/likes/互动率 降序优秀在前)→归因(✅爆款共性/❌差片共性/标题公式)，读 winning_patterns 标注为什么好/差 + 趋势历史 stats_history.jsonl
   queue_ops           视频库存队列：enqueue 入库 / dequeue 取待发 / archive 归档 / count 统计
   topic_gen           真实模型批量生成选题（topic/series/hook_type），供批量造片填充选题库
   ▼
fusion-agent-studio AgentRuntime       ── DAG 执行引擎（PR #105 已合并：output_mapping）
   ▼
graphs/douyin_auto_publish.json       ── 统一发布 DAG（高峰门控→有库存发布/无库存自动补货→归档，含审批门，验证用）
graphs/douyin_comment_reply.json       ── 评论回复 DAG（fetch→gate→draft→reply）
graphs/douyin_batch_produce.json       ── Graph C 批量造片单轮（script→img→tts→compose→enqueue，runner --loop N 循环）
graphs/douyin_queue_publish.json       ── Graph D 库存发布（dequeue→gate→publish→archive，runner --loop N 日上限）
out/queue/{pending,published,failed}/  ── 视频库存队列（pending 待发 / published 已发归档 / failed 失败归档）
```

## 关键设计点

- **扁平插值**：AgentRuntime 的 `VariableManager.interpolate` 只替换整段 `{{var}}`，不支持 JSON 路径。因此每个工具接收整段 `{{script_json}}`，由 `script_helpers` 抽取自己所需切片（`extract_prompts` / `extract_narration` / `extract_description`）。
- **列表变量传递**：工具输出列表经 `output_mapping` 写入变量后，`{{var}}` 会以 Python repr 形式插值（单引号）。`_parse_list` 用 `ast.literal_eval` 回退解析，兼容 JSON / repr / 逗号分隔 / dict 包裹（`{"prompts":[...]}`）。
- **Playwright async**：AgentRuntime 在 asyncio 事件循环内调用工具，Playwright **必须用 async API**（`async_playwright`），sync API 会报 "Sync API inside the asyncio loop"。
- **封面帧点击**：抖音封面选择器的父级 overlay div 会拦截真实指针事件，故 `_select_cover_frame` 改用 JS `element.click()`（与 `_open_cover_editor` / `_click_by_text` 同模式）绕过拦截。
- **审批门**：DAG 在发布前设 `condition` 节点 `safety_approved==true`，不通过则走 `end` 不发布；通过才进 `douyin_publish`。
- **dry_run**：`douyin_publish dry_run=true` 走完上传/描述/封面，停在「点击发布」前，返回 `status=dry_run_ok`。
- **Graph B 批量架构**：评论回复采用「抓取(1 浏览器)→草拟(1 次模型调用,≤5)→回复(1 浏览器)」批量设计，而非 runtime loop 逐条。原因：扁平插值不支持 JSON 路径，逐条 loop 在隔离会话下脆弱。`mlx_reply_draft` 把 `~/video/ops_monitor.py` 的 7 条 AUTO_REPLIES 规则注入为硬约束，模型仅在未命中时自由生成；`douyin_reply` 用 `out/ops/replied.json` 幂等去重、safe 过滤、≤5 上限。
- **运营统计报表**：`ops_report` 纯生成（不开浏览器），三段式结构——**①全貌**：账号粉丝/获赞 + 汇总播放/赞/评/分享/均互动率 + 表现分布(hot/mid/cold) + 钩子变体 A/B/C 均播放对比 + 趋势；**②逐视频细节排行**：按 `sort_by`(plays|likes|interaction_rate) 降序优秀在前，每条带 标题/播放/赞/评/分享/互动率/阶段/变体/为什么；**③归因总结**：读 `winning_patterns.json` 列爆款共性(✅)/差片共性(❌)/标题公式。细节行用 `winning_topics`/`losing_patterns` 关键词匹配标题标注「为什么好/差」。数据源以当次 `douyin_stats` items 为主、`stats_history.jsonl` 最新快照按 vid 补 stage/interaction_rate。落盘 `out/ops/report_YYYY-MM-DD.md`。

## 环境与依赖

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
cd fusion-operation
pip install -e ".[test]"
python -m playwright install chromium     # 首次需安装浏览器
```

依赖：httpx, playwright, opencv-python-headless, Pillow, numpy, edge-tts, fusion-core。
LLM 推理依赖 fusion-mlx：`~/claude-home/fusion-mlx/start.sh start`。

## 认证

fusion-mlx 启用 api_key 鉴权时，工具通过 `config.mlx_headers()` 取 key：
优先环境变量 `FUSION_MLX_API_KEY`，否则读 `~/.fusion-mlx/settings.json` 的 `auth.api_key`。
> ⚠️ 若环境变量里的 key 已过期/不匹配，会 401。`unset FUSION_MLX_API_KEY` 走 settings.json 回退。

抖音登录态：Playwright 持久化 profile `~/video/douyin_profile`（首次需扫码登录，之后复用 session）。

## 构建/测试

```bash
pytest tests/ -q                # 263 个用例全绿
pytest tests/test_douyin_tools.py::test_publish_dry_run_flow -v
ruff check .                    # 如配置
```

## REST API（issue #24）

`web_api.py` — FastAPI 薄层，graph 执行委托 agent-studio daemon RPC（与 cron/CLI 同路），无自有业务逻辑。端口 11431（port-registry: fusion-operation）。

```bash
python web_api.py               # 默认 127.0.0.1:11431，FUSION_OPERATION_API_PORT 可覆盖
```

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/health` | mlx 健康 + daemon RPC 可达性 + 队列计数 |
| GET | `/api/queue` | 队列计数 + pending/inflight 明细 |
| GET | `/api/scheduler` | daemon cron job 列表（注入 `_auth` JWT） |
| GET | `/api/graphs` | 本地 graph 模板清单 |
| POST | `/api/run` | `{graph, approve, vars}` 异步执行，返回 run_id |
| GET | `/api/runs` / `/api/runs/{id}` | 运行注册表 / 单次状态与结果 |

- **鉴权**：写端点（POST /api/run）要求 Bearer = 配置的 client key（issue #22 链）；无 key（匿名 dev）时放行但告警，仅 127.0.0.1 绑定。
- **daemon RPC auth**：daemon #326 起 UDS 强制 `_auth` JWT；`douyin_tools/rpc_auth.py` 用静态 module key（env `FUSION_AGENT_STUDIO_API_KEY` > `~/.fusion-agent-studio/web_module_key`）经 `auth.login` 换 JWT 并缓存。注意 `~/.fusion-agent-studio/env.local` 的同名变量会覆盖 start.sh 的 export——两处不一致会 `invalid apikey: key_not_found`。
- **UI**：控制面板页留给 fusion-web-shell 模块化阶段（本 issue 范围为 REST surface）。

## 运行 DAG

```bash
# 单条发布 dry_run（不真发，验证全路径；douyin_auto_publish 统一 Graph，含高峰门控+补货回环+审批门）
unset FUSION_MLX_API_KEY
python run_graph.py graphs/douyin_auto_publish.json true      # true=走 dry_run 发布路径验证

# 非高峰时段 → gate_peak 拦下走 end 延后（不发布，验证高峰门控路由）
python run_graph.py graphs/douyin_auto_publish.json true

# Graph B 验证（真实模型草拟 + reply dry_run + 条件路由，不开浏览器）
python verify_graph_b.py
```

`run_graph.py` 加载图 → `graph.validate()` → `create_douyin_registry()` → `AgentRuntime` → 流式执行 → 打印最终变量与末个工具结果。

## 批量造片 + 库存发布（方案 A）

生产策略：**每周日批量生成一批视频入库存，日常按日上限从库存发布**，解决"无库存/topic 硬编码/无发布节奏"三个缺口。

```
周日批量造片（Graph C，runner --loop N 外层循环）：
  topic_gen 真实模型产 N 条选题 → 逐条 script→img→tts→compose→enqueue 入 out/queue/pending/

日常库存发布（Graph D，runner --loop N 日上限）：
  逐条 dequeue 取 pending → gate has_stock → publish → archive 归档；库存空则结束
```

```bash
# 周日批量造片 7 条（先 topic_gen 产选题，再逐条造片入库）
unset FUSION_MLX_API_KEY
python run_graph.py graphs/douyin_batch_produce.json true --loop 7 --series 宇宙奥秘

# 日常发布今日 2 条（从库存 pending 取，dry_run=true 不真发可先验证路由）
python run_graph.py graphs/douyin_queue_publish.json true --loop 2
# 真实发布（approve=true 且 graph 内 dry_run 由 runner 设 false）
python run_graph.py graphs/douyin_queue_publish.json true --loop 2

# 查库存
python -c "import asyncio,json;from douyin_tools.queue_ops import QueueOpsTool;print(json.loads(asyncio.run(QueueOpsTool().execute(action='count'))))"
```

**库存结构**（`out/queue/`）：`pending/` 待发 · `published/` 已发归档 · `failed/` 失败归档，每项一个 JSON `{id, video_path, script_json, topic, title, description, status, created_at}`。

**关键约束**：`queue_ops` 的 `has_stock` 输出为字符串 `"true"`/`"false"`（非 JSON bool），适配 ConditionEngine 的字符串比较（`has_stock == true`，同 Graph A 的 `safety_approved` 模式）。dequeue 同时输出扁平字段 `video_path`/`script_json`/`title`/`item_id`，规避 AgentRuntime 不支持 JSON 路径插值的约束。

## 验证状态（生产商业发布标准）

| 阶段 | 状态 |
|---|---|
| 10 工具实现 + 35 单测 | ✅ 全绿 |
| 12 工具实现 + 44 单测 | ✅ 全绿（新增 queue_ops / topic_gen） |
| 13 工具实现 + 48 单测 | ✅ 全绿（新增 perf_track 进化数据闭环） |
| 16 工具实现 + 63 单测 | ✅ 全绿（新增 perf_analyze/self_repair/publish_scheduler 进化 Phase 2-5） |
| topic_gen 真实模型批量选题 | ✅ 5 选题不重复，含 hook_type |
| queue_ops 库存队列 enqueue/dequeue/archive/count | ✅ 文件落盘+归档验证 |
| Graph C 批量造片单轮（真实模型 script→img→tts→compose→enqueue） | ✅ 入库 pending（见下方） |
| Graph D 库存发布条件路由（has_stock true/false） | ✅ true→发布+归档，false→结束 |
| Graph A 全 DAG（真实模型脚本→配图→配音→合成） | ✅ completed=True，产出 1080×1920 mp4 |
| 审批门路由（true/false） | ✅ |
| douyin_publish dry_run 全路径（真实浏览器+已登录会话） | ✅ status=dry_run_ok verify=true，停在发布前 |
| Graph B 真实模型草拟→reply dry_run 计划 | ✅ 5 评论 5 safe，AUTO_REPLIES 规则命中正确 |
| Graph B 条件路由（has_new true/false） | ✅ false→end 跳过 draft，true→draft 执行 |
| ops_report 三段式统计报表（全貌+细节排行+归因） | ✅ 真实数据 117 视频排行落盘，✅爆款/❌差片归因标注，variant A/B/C 对比 |
| douyin_fetch_comments 真实浏览器抓取（已登录会话） | ✅ 登录态有效，解析真实评论（用户/文本，has_new 判定） |
| douyin_reply 真实浏览器回复路径 | ✅ L3 已回复评论 id=4dc45700f296；幂等复跑 todo=0 不重复 |
| douyin_stats 真实抓取（已登录会话） | ✅ 登录态有效，92 作品+账号粉丝37/获赞324；play/like/share 已补齐（item/list API 取 vid/title/comment_count + 页面正文滚动解析 播放/点赞/分享，52/92 带真实播放量） |
| 真实发布（dry_run=false） | ✅ 3 bug 修复并端到端验证：精确点击底部"发布"主按钮（避开"高清发布"误匹配触发离开弹窗）+ `_parse_bool` 解析字符串 dry_run + `_dequeue` 扁平 desc_text 纯描述（旧 `{{script_json}}` 把 dict repr 塞进描述框）。Graph D `gate_ok`：publish_ok 才归档 published，失败留 pending。作品数 92→95 实发 3 条，stats 二次验证描述纯文本 |
| **Phase 1 数据闭环（进化机制）** | ✅ `perf_track` 工具（snapshot+rank）：95 条全量落盘 stats_history.jsonl（按 vid 累积多时间点快照+生命周期 stage+interaction_rate）；rank 分类 hot/mid/cold（19/35/41）；Graph E `douyin_evolve` 端到端跑通；发现首条破千视频（1329 播放，结尾选择题钩子互动率最高）。详见 `~/video/video-enhance.md` |
| **Phase 2 归因回灌** | ✅ `perf_analyze` 真实模型归因：读取 stats_history（ranked）+ published script_json，模型抽取 winning_patterns（winning_topics/winning_hooks/losing_patterns/title_formula/topic_gen_hint）落盘 `out/ops/winning_patterns.json`。真实数据归因结果与人工观察吻合（极端数字+反常识、第二人称代入、反常识否定句）。回灌闭环：`topic_gen` 读 patterns+blacklist+published 排重注入 hint；`mlx_script` 注入 winning_hooks 约束脚本生成 |
| **Phase 3 A/B 变体** | ✅ `mlx_script` 增 `hook_variant` 参数（A=数字+反常 / B=提问+代入 / C=悬念+冲突），变体标记写入 script_json→published；`perf_track` rank 增 `by_variant` 聚合（count/plays_avg/plays_max）+ vid 反查回填（按标题模糊匹配 stats vid→published）。可量化 A/B/C 钩子效果 |
| **Phase 4 自修复** | ✅ `self_repair` 工具（action=scan/retitle/reclip/blacklist）：scan 扫 stats_history 找 age≥72h & plays<100 & interaction_rate<0.01 的差片（且 vid 在 published 存在，避免视频文件已删）；retitle 真实模型换标题重入 pending（复用原视频文件，不重渲染）；reclip 按用户决策标记 skip（仅换标题重发）；blacklist 写 topic_blacklist.json。Graph F `douyin_repair_publish`：scan→gate_candidate→retitle 条件路由。真实模型 retitle 验证：「夸克汤是什么」→「100 万人只喝 1 滴！夸克汤竟能吸走所有病毒？」（套用 title_formula） |
| **Phase 5 高峰调度** | ✅ `publish_scheduler` 工具：高峰窗口 9-10/11-12/14-15/18-19（4 发布窗口，对齐 cron 节奏），输出 in_peak/should_wait/next_window。Graph D v2 增 scheduler 节点 + gate_peak（in_peak==true 才 dequeue 发布，false 结束）。`run_graph.py` off-peak 自动 break。真实路由验证：非高峰→stop，next_window 指向下一窗口 |
| **OODA 全闭环** | ✅ Graph E v2 `douyin_evolve`：start→snapshot→rank→analyze→repair_scan→end（Observe→Orient→Decide→Act 全链）。6 个 DAG 全部 schema 校验通过。16 工具 + 63 单测全绿 |
| **cron 节奏化运营** | ✅ 8 cron job 进 daemon cron.db：4 发布（09:05/11:05/14:05/18:05，Graph `douyin_auto_publish`）+ 2 评论（13:35/21:35，Graph B）+ 1 进化（23:15，Graph E）+ 1 修复（23:45，Graph F，evolve 后跑）。`douyin_auto_publish` 统一 Graph：高峰门控→有库存发布/无库存自动补货（造片链回环 dequeue）→归档。上游 cron 运行时死代码已修（issue #139 → PR #140 已合并）。真实 cron 触发验证：`cron.list_executions` status=success events=9（真发布完整链路） |
| **launchd 持久化** | ✅ daemon + mlx 双 launchd KeepAlive（`~/Library/LaunchAgents/com.fusion-{agent-studio,mlx}.server.plist`），系统重启/进程 crash 自动拉起。8-17 全天 0 发布根因（daemon 被停无人拉起→cron 全错过）已修。`start.sh install-launchd` 装载，`uninstall-launchd` 卸载。mlx launchd 空数组 `set -u` 崩溃已修（issue #560 → PR #561 已合并 main） |
| **plugin venv 依赖对齐** | ✅ `sync_venv_deps.py` 幂等拷贝 cv2/playwright/greenlet/pyee 进 agent-studio 独立 venv（8-18 全天 0 发布第二根因：独立 venv 缺依赖→12 plugin 加载失败→publish_scheduler 缺失→gate_peak 拦下）。上游 issue #164 → PR #165 已落地 `plugins-extra` optional-dependency + `failed_plugins` 显式化 |
| **SafetyGateway tool_call policy** | ✅ 上游 issue #205 → PR #206 已合并 main：`_DEFAULT_POLICIES` 加 `CAT_TOOL_CALL` L1 auto-approve policy。修 8-24 cron 0 发布根因（无 tool_call policy→missing-policy BLOCK→60s timeout→所有 type:tool 节点全挂，与 level 无关）。内容 `check()` 仍跑破坏性 pattern 拦截，无安全降级。**注意**：CLI 本地路径不构造 safety_gateway 故不阻塞，排查勿只跑本地 dry_run 误判 |
| **graph stop_on_tool_error** | ✅ 上游 issue #202 → PR #203（commit 9cb3f67）+ PR #204 持久化修复 已合并：`AgentGraph.stop_on_tool_error` opt-in flag，工具错误 stop cascade + ctx.error + ERROR event metadata `tool_error=True`。下游 8 graph 已加 `stop_on_tool_error: true`。配套 PR #206 修 safety 阻塞（不修 #206 则显式化的全是 safety timeout） |
| **graph_id 漂移回填** | ✅ `sync_graphs.py` 重建 graph 后 `_backfill_cron_graph_id` 用 `cron.register`（INSERT OR REPLACE）回填关联 cron job 的 graph_id（8-20 11:05 根因：cron 绑死旧 id→`Graph not found`）。上游 str params 崩溃已修（issue #172 → PR #173 已合并，`_dispatch` 单点规范化 str→dict） |
| **MLX 推理引擎 launchd** | ✅ `fusion-mlx/start.sh install-launchd` 开机自启 + KeepAlive（`com.fusion-mlx.server.plist`）。8-24 根因：mlx 无 launchd → 系统重启/crash 即丢 → 下游 cron 全 502。上游 `set -u` 空数组崩溃已修（issue #560 → PR #561 已合并 main，`${arr[@]+"${arr[@]}"}` 安全展开）。KeepAlive kill-9 硬测自动拉起。3 核心服务（mlx+daemon+comfyui）均 launchd 持久化 |
| **comfyui launchd 持久化** | ✅ `fusion-comfyui/start.sh` 加 `install-launchd`/`uninstall-launchd`（`com.fusion-comfyui.server.plist` RunAtLoad+KeepAlive）。8-24 补货链根因：comfyui 进程不在 → 造片链 `comfyui unavailable` stop cascade → pending 持续 0 → 4 发布 cron 空跑。与 mlx/daemon 同模式。上游 issue #28 → PR #51 提交待 review |
| **perf_analyze 静默失败修复** | ✅ `perf_analyze._call_model` 基础设施错误（ConnectError/Timeout/NetworkError/http≠200）→ raise RuntimeError → graph `stop_on_tool_error` trip 停级联（不再静默写空/旧 patterns）。解析错误仍 soft-return None 保留旧 winning_patterns.json。根因：mlx 不可达时模型调用吞异常 → winning_patterns.json 4 天不更新（8-20→8-24）→ 进化闭环 OODA 断在 Orient。真实验证：RPC 跑 evolve，patterns mtime 刷新 updated_at=2026-08-24 samples=152 hot=30 |
| **补货链端到端验证** | ✅ `douyin_auto_publish` 无库存自动补货全链路 RPC 验证（27 events）：scheduler(in_peak)→dequeue(空)→gate_stock(false)→refill 分支 topic_gen→mlx_script(真实模型 5 分镜 variant=A)→comfyui_image(5 张 1.9-2.4MB)→comfyui_tts(Qwen3 http 1.3MB WAV)→ffmpeg_compose(6.8MB mp4 dur=27.5)→queue_ops enqueue(pending=1)→gate_refilled(true)→dequeue→publish。产物全真实。comfyui+mlx launchd 恢复后补货不再断 |
| **差片修复闭环 + scan 去重** | ✅ `douyin_repair_publish`（Graph F）进 cron 23:45（evolve 23:15 之后跑，scan 见最新 rank）。OODA Act 阶段补齐：scan 扫 72h 差片→gate_candidate→retitle 模型换标题→落 `review/` 待人工确认（L3 安全：同 video bytes 重传抖音去重/限流，不自动进 pending）。`self_repair._scan` 加 `_review_vids` 去重——跳过 review/ 已 retitle 的 vid，避免夜夜重复 retitle 堆积（cron 重复扫描根因）。真实验证：scan candidates 25→21（跳 4 个 review vid），retitle 真产出新标题落 review。8 cron job（4 发布+2 评论+1 进化+1 修复），111 单测全绿 |
| **对抗审查修复（致命层+逻辑+性能）** | ✅ 113 单测全绿（+2 C6 防回归）。致命层：① config.py 模块级 `_cfg_mtime` 定义在调用后→NameError 崩全模块 import→CLI/cron/测试全死（已修顺序）+ `_resolve_mlx_api_key` mtime 缓存（每条造片链多次 mlx_headers 读盘→缓存复用）+ `_refresh_if_stale` mtime 失效缓存（daemon launchd 长进程改 douyin_config.json 不重启即生效）；② `encoding="utf-8"` 跨 9 文件+run_graph 2 处（launchd 不继承 shell locale→macOS py3.12 `getpreferredencoding` 回退 US-ASCII→读中文 JSON/graph UnicodeDecodeError→进化/统计/回复链崩）；③ 原子写 perf_analyze winning_patterns + ops_report report（crash 残留半截 JSON→下游解析崩→进化闭环断）；④ ffmpeg_compose run_in_executor（同步 render 阻塞 daemon 事件循环）+ 失败短路（坏 seg/concat/mux 不再静默产坏 mp4→抖音黑屏）+ 字幕强制断行；⑤ douyin_publish C6 `_verify_by_count` before<0 return True→失败发布误判成功→误归档 published→A/B 归因污染+stats 虚高+self_repair 扫不到真实 vid（改 return False+vid 兜底验证）。逻辑层：self_repair `_find_published` vid 精确匹配、queue_ops _archive TOCTOU。性能/维护：self_repair `_extract_json` 括号配平+多候选尾部修复（模型输出残缺尾部 `]` 误替 `}`→parse failed→造片整轮失败）、run_graph RPC 超时（上游 AgentClient.call 无 timeout→daemon 卡死 reader.readline 永久 await→cron 永久挂起叠加下次 fire）。真实验证：daemon 56 tools 0 failed_plugins，8 cron job 完好，config/api_key mtime 缓存运行时验证（改 fps 读回生效）。**残留风险补修**：6 处非原子写补 temp+os.replace（queue_ops enqueue/archive、self_repair retitle/blacklist、perf_track backfill、web_image_search meta），crash 中途不留半截 JSON 卡死下游解析。**上游 RPC timeout 已根治**：[agent-studio #207](https://github.com/dahai80/fusion-agent-studio/issues/207) — `AgentClient.call` 无 timeout 参数，daemon 无响应时 `reader.readline()` 永久 await，CLI/cron 挂死。上游 [PR #208](https://github.com/dahai80/fusion-agent-studio/pull/208) 已合并 main（`__init__(default_timeout=None)` + `call(timeout=None)` + `asyncio.wait_for` 包 readline + TimeoutError 返回 error dict，默认 None 向后兼容）。下游 `run_graph.py` 已移除 CLI 层 `_rpc_call` wait_for 包装，改 `AgentClient(socket_path, default_timeout=RPC_TIMEOUT)` 用上游 native timeout（`FUSION_RPC_TIMEOUT` 默认 600s）。daemon 重启加载新 SDK（PID 82434，launchd KeepAlive），56 tools 0 failed_plugins，8 cron job 完好 |

### 已知上游问题（fusion-mlx，已提 issue + PR）
- `/v1/images/generate` 默认模型 `flux-2` 返回 500（别名未注册，应解析到 `flux2-klein-9b-4bit`）→ 工具回退静态图（不阻塞生产）。已提 PR #448 修复（注册别名 + 图像路由走 `resolve_model_id` + 404）。[#446](https://github.com/dahai80/fusion-mlx/issues/446)
- `/v1/audio/speech` 默认模型 `kokoro` 失败（注册表无该模型，可用 TTS 为 `Qwen3-TTS-12Hz-1.7B-Base-8bit` / `kitten-tts-nano-0.8`）→ 工具回退 edge-tts（不阻塞生产）。已提 PR #448 修复（注册 `kokoro` 别名，真实验证产出 76KB WAV）。[#447](https://github.com/dahai80/fusion-mlx/issues/447)
- 下游工具默认值 `flux-2`/`kokoro` 与上游 PR #448 注册的别名对齐，合并重启后无需改下游代码即可命中真实模型。

## 整体运营（fusion-studio 集成）

抖音吸粉全链路已迁移到 fusion-studio GUI 运营，无需命令行。fusion-studio「抖音运营」看板（`DouyinOperationView`）经 IPC `graph.create`+`graph.execute` 调 agent-studio daemon 跑运营 DAG：

| 看板按钮 | 调用 Graph | 运营动作 |
|---|---|---|
| 开始造片 | `douyin_batch_produce`（Graph C） | 脚本→配图→TTS→合成→入库，支持手填选题 + A/B/C 钩子变体 |
| 真实发布/Dry-run | `douyin_auto_publish`（统一 Graph） | 高峰门控→有库存发布/无库存自动补货（造片链回环）→归档，cron 4 次/天自动触发 |
| 评论回复 | `douyin_comment_reply`（Graph B） | 抓评论→草拟→批量回复（幂等），cron 2 次/天自动触发 |
| 运行进化闭环 | `douyin_evolve`（Graph E） | 快照→排名→爆款归因→差片扫描，cron 1 次/天（23:15）自动触发 |
| 扫描并修复 | `douyin_repair_publish`（Graph F） | 差片换标题重入 review 队列待人工确认，cron 1 次/天（23:45，evolve 后）自动触发 |

看板实时展示：待发布/已发布/失败队列计数、爆款模式（winning_patterns）、按播放量排序的统计快照。

### 上游服务自动管理

fusion-studio 启动时 `UpstreamServiceManager.ensureCriticalRunning()` 按依赖顺序自动拉起关键服务（缺则调对应 `start.sh start`，失败则展示状态）：

- **MLX 推理服务**（`fusion-mlx`，order 0，`/health`）— 核心
- **ComfyUI 造片服务**（`fusion-comfyui`，order 1，`/system_stats`）— 造片链 Graph C 依赖（文生图+TTS）
- **Agent Studio Daemon**（`fusion-agent-studio`，order 1，UDS socket）— RPC 运行时
- Artifacts Engine（order 2，关键）+ rag/multi-node/cowork 等（非关键，手动启停）

### 运维修补：鉴权同源

`fusion-comfyui/start.sh` 与 `fusion-agent-studio/start.sh` 启动时 `unset FUSION_MLX_API_KEY`，清掉 shell 继承的过期 key，统一走 `~/.fusion-mlx/settings.json` auth.api_key。避免 daemon/comfyui 进程携带过期 key 调 fusion-mlx 触发 401（造片链 mlx_script + TTS http backend 均受影响）。验证：带 stale key 重启两服务，进程 env 无 `FUSION_MLX_API_KEY`，Graph C RPC 端到端 `completed=True` 无 401。

> CLI 调试入口 `run_graph.py` 是本地 Python 进程，直接继承 shell env，需 `env -u FUSION_MLX_API_KEY` 启动；生产路径（fusion-studio → daemon，经 start.sh unset）不受影响。

## 声明式配置（改配置不改代码）

业务参数集中在根目录 `douyin_config.json`，工具启动时读取。**优先级：环境变量 > JSON 配置 > 硬编码默认**，三级回退保证向后兼容。`paths.*` 一律写**相对仓根路径**（`out/videos`）或 `~/` 前缀（`~/video/douyin_profile`）：`config.py` 统一 `_resolve_path` 解析（`~` 展开到当前用户 home，相对路径按仓根 join），禁止绝对个人路径（CI runner 无本地 home，绝对路径 makedirs 直接 PermissionError；`deploy_check.py` + 回归测试双门禁拦截）。

可配置项：

| 段 | 字段 | 说明 |
|---|---|---|
| `models` | chat/tts/tts_voice/image | 默认模型与音色 |
| `comfyui` | base/timeout/output_dir/image_model/tts_* | ComfyUI 造片服务地址与模型 |
| `mlx` | base/timeout/settings_path | fusion-mlx 推理引擎地址与鉴权文件 |
| `paths` | video/audio/image/ops/queue/topics/douyin_profile | 产物与登录态目录 |
| `script` | max_scenes / hook_variants(A/B/C) | 脚本分镜上限、钩子变体定义 |
| `scheduler` | peak_windows | 发布高峰窗口（默认 9-10/11-12/14-15/18-19，对齐 4 发布 cron 节奏） |
| `self_repair` | cold_plays_threshold / cold_interaction_rate_threshold / cold_age_hours | 差片修复阈值 |
| `perf_track` | hot_top_percent / hot_plays_min / cold_plays_max | 爆款/差片分类阈值 |
| `agent_studio` | socket / agent_id / tool_config | daemon socket、抖音运营官智能体包 id、工具级配置注入映射 |
| `video` | fps / width / height | 视频规格 |

改 JSON 后重启 daemon 即生效（`mlx_script.MAX_SCENES`、`publish_scheduler.PEAK_WINDOWS`、`self_repair.REPAIR_*`、`perf_track.HOT/COLD_*` 均从配置读）。自定义配置文件路径：`DOUYIN_CONFIG_FILE` 环境变量。

### 工具级配置注入（声明式智能体包）

`agent_studio.tool_config` 段用**引用式**配置把模型/音色映射进智能体包的工具默认参数：

```json
"agent_studio": {
    "agent_id": "c65efddbe8c5",
    "tool_config": {
        "mlx_script":    {"model": "models.chat"},
        "comfyui_tts":   {"voice": "comfyui.tts_http_voice"}
    }
}
```

`"models.chat"` 引用解析为 `cfg["models"]["chat"]` 的实际值。`sync_agent_config.py` 读 `douyin_config.json` + 智能体包 `manifest.json`，生成 `~/.fusion-agent-studio/agents/{agent_id}/definition.json`（含 `tools[].config`）。`run_graph.py` 把 `agent_id` 注入 `graph.execute`，daemon 读 `definition.json` → `set_tool_configs` → 工具执行时作为默认参数合并（caller args 覆盖）。改 `models.chat` 后 `python sync_agent_config.py` 重新生成即生效，不改代码。

```bash
python sync_agent_config.py     # 生成/刷新 definition.json
```

> 上游已落地：[issue #125](https://github.com/dahai80/fusion-agent-studio/issues/125) → [PR #126](https://github.com/dahai80/fusion-agent-studio/pull/126) 已合并，`AgentToolConfig` 加 `config` 字段，daemon `graph.execute` 按 `agent_id` 加载 `definition.json` 注入工具配置。

### 运营 graph 预注册（agent-studio GUI 可见可管理）

agent-studio daemon 的 graph 存在 SQLite `graphs` 表（`graph.list` 从此读），不扫描文件目录。`graphs/*.json` 只是源文件，fusion-studio 看板运行时按需 `graph.create`（用完缓存复用），不持久化 → agent-studio GUI 看不到稳定的运营 graph。`sync_graphs.py` 把 7 个运营 graph 按 name 去重（同名先删后建）注册进 daemon store，幂等不膨胀。

```bash
python sync_graphs.py          # 把 graphs/*.json 注册进 daemon store，agent-studio GUI 即可见
```

## 目录

```
douyin_config.json   声明式业务配置（模型/路径/阈值/钩子变体/高峰窗口，改配置不改代码）
sync_agent_config.py 声明式智能体包同步（douyin_config.json → agents/{id}/definition.json 工具配置注入）
sync_graphs.py       运营 graph 预注册进 daemon store（agent-studio GUI 可见可管理，幂等去重 + cron graph_id 回填）
sync_venv_deps.py    plugin venv 依赖对齐（拷贝 cv2/playwright 进 agent-studio 独立 venv，幂等）
douyin_tools/        工具实现
  config.py          常量/目录/鉴权/日志（读 douyin_config.json：env > json > 默认三级回退，mlx_headers 带 X-Fusion-Route）
  script_helpers.py  扁平插值所需的脚本切片抽取（parse_script 兼容 dict repr/JSON 双路径）
  comfyui_client.py  ComfyUI HTTP 客户端（health/submit/wait_result/run_graph）
  comfyui_produce.py comfyui_image 文生图（FusionImageGen，竖屏 1080x1920，禁人物 negative_prompt）
  comfyui_tts.py     comfyui_tts 语音合成（Qwen3-TTS http backend，中文音色 Cherry）
  mlx_script.py      脚本生成（hook_variant A/B/C + winning_hooks 注入 + 禁人物约束 + narration_segment 音画同步）
  mlx_image.py       文生图（fusion-mlx 直连，回退）
  mlx_tts.py         语音合成（fusion-mlx 直连，回退）
  web_image_search.py  NASA 真实天体图搜索（公共领域商用合规，真实图优先于生成图）
  ffmpeg_compose.py  视频合成（竖屏 + narration_segment 按字数比例分配时长 + 星球占位兜底非黑首帧）
  douyin_publish.py  Playwright 异步发布（profile 锁硬化 + 首帧封面 + vid 捕获）
  douyin_fetch_comments.py  抓取评论
  mlx_reply_draft.py        模型草拟回复
  douyin_reply.py           批量点赞+回复
  douyin_stats.py           抓取统计
  ops_report.py             运营统计报表（全貌+细节排行+归因）
  perf_track.py             表现追踪（snapshot 落盘+rank 排行分类+by_variant A/B/C 聚合）
  queue_ops.py              库存队列 enqueue/dequeue/archive/count（vid 透传）
  topic_gen.py              真实模型批量选题（读 winning_patterns+blacklist+published 排重回灌）
  perf_analyze.py           归因工具：模型抽取 winning_patterns（Phase 2）
  self_repair.py            自修复：scan 差片/retitle 换标题重入/blacklist（Phase 4）
  publish_scheduler.py      高峰调度：9-10/11-12/14-15/18-19 窗口判定（Phase 5）
  __init__.py        create_douyin_registry()（19 工具）
graphs/              DAG 定义（JSON，含 douyin_auto_publish 统一发布+补货 Graph）
tests/               pytest
run_graph.py         本地验证 runner（RPC 优先，本地回退；--loop N 批量/日上限）
verify_graph_b.py    Graph B 验证（真实模型+条件路由）
out/                 产物（video/audio/image/ops），.gitignore
```

