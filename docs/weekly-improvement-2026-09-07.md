# 周运营分析与改进 — 2026-09-07

> 数据范围：2026-08-22 ~ 2026-09-07（过去两周）。数据源：`out/ops/stats_history.jsonl`（168 unique vid，最新快照 09-06 23:15）、`out/queue/published/`（49 published）、cron.db（62 executions）、self_commented.json。

## 一、好的方面

1. **破千达成**：max 播放 1149（variant A「1克夸克踩脚」），首次单视频破 1000。Top2=995（variant B 白矮星茶杯），Top3=941（旧片恐龙）。新系统片 top1-2 全包。
2. **A/B 闭环有信号**：variant A（数字+反常）max 1149 avg 97.9（22 样本），B（提问+代入）max 995 avg 99.5（10），C（悬念+冲突）max 0 avg 0（4）。C 变体 0 播放 → 应淘汰/调整。
3. **爆款公式验证**：winning_patterns.json（9-5 产出，33 hot）—— winning_topics=[极端微观/宏观物体与身体部位直接物理冲突（1克夸克踩脚、中子星塞鼻孔）, 具体量化参数灾难后果（太阳近1%、月球近10倍）]。Top1「1克夸克踩脚」= 公式直接命中。
4. **cron 持续运行**：9 cron job 全 enabled，FUSION_CRON_TZ=Asia/Shanghai，9-7 0905/1105 publish success events=11/15（造片+发布全链路）。9-5/9-6 0 发布根因已修（mlx 停 + launchd + peak 缺 18-19 + GPU 争用）。
5. **持久化落地**：daemon + mlx 双 launchd KeepAlive，系统重启自动拉起，9-7 待办清零。
6. **vid 捕获准确**：49 published 全有 vid（no_vid=0），A/B 闭环不再依赖事后标题模糊匹配。self_comment 4 vid 真发首评。
7. **质量改进在生效**：新片（variant 标记的）带关注引导、禁人物、竖屏配图、星球兜底、音画同步、BGM+封面标题 overlay。Top1-2 都是新系统片，旧模板片（「用AI把知识画出来」污染）被新片超越。

## 二、不好的方面

1. **93% 零播放**：168 unique vid 中 157 个 0 播放（93%）。仅 11 个 100-999。这是最大问题——不是代码 bug（发布确实成功 vid 捕获），是抖音有机分发天花板 + 内容/选题问题。
2. **库存空**：pending=0，failed=0，inflight=0。下一次 cron publish 必须先造片（耗时 3-5min/条），高峰窗口刚触发时可能造片未完错过窗口。
3. **topic_blacklist 缺失**：`out/ops/topic_blacklist.json` 不存在 → `topic_gen` 实体级去重没有黑名单输入，可能重复选题。
4. **winning_patterns 陈旧**：updated_at=2026-09-05，2 天未刷新，samples=168 但 hot_count 仅 33。09-6 evolve cron `timed out after 300s` 失败 → 没产出新 patterns。topic_gen/mlx_script 注入的是 2 天前的模式。
5. **evolve/repair cron 超时**：09-6 23:15 evolve + 23:45 repair 均 `job timed out after 300s`。300s 上限对 27B 模型 + 168 样本归因不够。需提升 cron job timeout。
6. **发布节奏断层**：9-4/9-5/9-6 三天 0 发布（已修根因），节奏从 8 月初 11/天降到 3-5/天。库存跟不上 5 窗口发布频率。
7. **C 变体全 0**：4 个 C（悬念+冲突）变体片全 0 播放。样本少但方向性明确——C 钩子形式不吸抖音推荐。

## 三、改进措施（立即落地）

### 措施 1：补库存（pending=0 → 补 5+）
立即跑造片链补 5 条 variant A/B（跳过 C，C 全 0 待淘汰）。保证下一次 cron publish 有库存直接发，不卡在造片。

### 措施 2：重建 topic_blacklist
从 49 published titles 提取已用实体写入 `out/ops/topic_blacklist.json`，让 topic_gen 实体级硬去重生效，避免重复选题（太阳压缩×6 全 0 教训）。

### 措施 3：刷新 winning_patterns
跑 evolve（perf_analyze）对当前 168 样本重新归因，产出新鲜 winning_topics/hooks → topic_gen hint + mlx_script winning_hooks 注入。

### 措施 4：evolve/repair cron timeout 提升
09-6 evolve/repair `timed out after 300s`。cron job 默认 300s 上限对 27B 归因不够。查 cron register 是否支持 timeout 配置，或降级 evolve 用更小模型/分批。

### 措施 5：淘汰/调整 C 变体
C 变体 4/4 全 0。`run_graph run_batch_produce` 轮转 `["A","B","C"][i%3]` 改为 `["A","B","A","B"]` 加权，C 待样本验证后决定是否保留。

## 四、落地验证

见各任务执行结果。测试 `pytest tests/ -q` 预期 150 全绿。
