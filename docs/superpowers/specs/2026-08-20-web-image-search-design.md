---
doc_id: fusion-operation/web-image-search-design
product: fusion-operation
class: app
entry: service
doc_type: guide
title: { zh: "fusion-operation 网图搜索设计", en: "fusion-operation web image search design" }
audience: engineer
status: draft
version: 0.1.3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/superpowers/specs/2026-08-20-web-image-search-design.md
---

# web_image_search 工具设计 — NASA 真实天体图优先

## 背景与目标

fusion-operation 抖音吸粉账号讲天体/星球科普。当前造片链全靠 comfyui/comfyui_image 文生图，配图是 AI 生成。问题：

1. 真实天体图（NASA 实拍地球/木星/星云）比 AI 生成图更可信、更有视觉冲击，观众一眼能区分"真实 NASA 图"vs"AI 套图"，真实图点击欲更高。
2. AI 生成图偶发退化（1024x3 黑图，上游 comfyui issue #37 未修）、暗调首镜黑底（已用星球占位图兜底但仍是占位非真实）。
3. 用户明确要求：「不用全部你自己生成图片和视频，你也可以从网上搜索星球，天体的图片和视频作为你的输入」。

目标：新增 web_image_search 工具，按天体关键词搜 NASA Image Library（公共领域，商用合规），下载真实图到 out/images，Graph C 造片优先用真实图，搜不到/不足回退 comfyui 生成图。本轮只做图片，不做视频片段。

## 范围

**做**：
- 新工具 `web_image_search`：调 NASA Image Library REST API 搜图、下载、落盘、返回 image_paths。
- 关键词提取：从 mlx_script 产出的 scenes[].img_prompt 提取天体主体名作 NASA query。
- Graph C (`douyin_batch_produce.json`) 接入：script 后加 web_image_search 节点 + gate 条件门，真实图优先，不足回退 comfyui_image 生成。
- 声明式配置：NASA API base/超时/每 query 取图数进 douyin_config.json。
- 单测（mock NASA API）+ 真实 NASA API 端到端验证。
- 部署：sync_plugins + sync_graphs + launchd restart。

**不做（out of scope）**：
- 视频片段搜索/拼接（ffmpeg_compose 不改，仍只吃 still image_paths）。
- 搜索引擎抓图（Bing/百度，版权风险）。
- Wikimedia/ESA 等其他图库（先接 NASA 一个，后续可扩展）。
- 图片裁剪/调色后处理（NASA 原图直接落盘，ffmpeg_compose 的 _scene_base 已有 _load_full 等比缩放裁切 + 坏图兜底）。

## NASA Image Library API

官方文档：https://images.nasa.gov/docs

**免 key，公共领域**：NASA 工作产物（含实拍图）属美国联邦政府工作，无版权，商用合规。

**搜索端点**：
```
GET https://images-api.nasa.gov/search?q={query}&media_type=image&page_size=20
```
响应 JSON 结构：
```json
{
  "collection": {
    "items": [
      {
        "href": "https://images-api.nasa.gov/assets/AS11-44-6552",   // metadata JSON url
        "data": [{"nasa_id": "AS11-44-6552", "title": "...", "photographer": "...", "description": "..."}]
      }
    ]
  }
}
```

**取原图**：每个 item 的 `href` 指向 metadata JSON，GET 之返回一个 URL 数组，按文件后缀选最大/合适格式：
```
GET {item.href}  →  ["...~orig.jpg", "...~large.jpg", "...~medium.jpg", "...~thumb.jpg", "metadata.json"]
```
选 `~large.jpg`（NASA 按尺寸分档，large 质量足够竖屏裁切，orig 太大占磁盘）。

**超时/限流**：NASA API 无官方限流文档，实测宽松。设 httpx timeout 20s，单 query 取图数 ≤ 5。失败（http 非 200/空 items/网络错）→ 该 query 返回空，不阻断（上层回退生成图）。

## 工具设计 (web_image_search)

文件：`douyin_tools/web_image_search.py`，类 `WebImageSearchTool(BaseTool)`。

```python
name = "web_image_search"
description = "搜 NASA Image Library 真实天体图并下载，返回 image_paths JSON（comfyui_image 的真实图优先路径）"
parameters = {
    "queries": {"type": "string", "description": "天体英文关键词列表(JSON 数组或逗号分隔)"},
    "per_query": {"type": "integer", "description": "每个关键词取几张，默认 1"},
    "out_prefix": {"type": "string", "description": "落盘文件名前缀，默认 web_img"},
}
```

**契约**：返回 `{"image_paths": ["/abs/path/web_img_000.png", ...]}` JSON，与 comfyui_image/mlx_image 一致，Graph C 的 img 节点 output_mapping 无需改。

**execute 流程**：
1. `queries` 支持 script_json（复用 script_helpers.parse_script + extract_prompts 提取 img_prompt，再抽主体词）或裸关键词列表。
2. ensure_dirs()。
3. 对每个 query：调 NASA search → 取首个 item 的 metadata JSON href → GET href → 选 `~large.jpg` → 下载到 `out/images/{out_prefix}_{idx:03d}.png`。
4. 单 query 任一步失败 → 记 warning，该位不产出（image_paths 可能少于分镜数）。
5. 返回 image_paths（可能不足分镜数，上层 gate 判不足触发回退）。

**方法拆分**（隔离可测）：
- `execute(queries, per_query, out_prefix)`：编排。
- `_parse_queries(val)`：script_json/列表/逗号串 → query 列表（复用 _parse_list 模式）。
- `_search_nasa(query)` → list[dict]：调 search 端点，返回 items（mock 注入点）。
- `_resolve_image_url(item_href)` → str：GET metadata JSON，选 large jpg url（mock 注入点）。
- `_download(url, dst)` → bool：httpx 下载落盘。

**logging**：每步 NASA 搜索 query/响应 item 数、选图 url、下载成功/失败、最终产出数。回退时记 warning。

## 关键词提取

NASA 搜索用简短天体名词（"jupiter"）比完整 img_prompt（"A massive bright orange star on the left side approaching a super massive black hole..."）召回更准。两种来源：

1. **Graph C 直接传裸 query 列表**：run_graph 的 `run_batch_produce` 按 topic 串注入，如 topic="木星大红斑" → query="jupiter great red spot"。需在 Graph C 节点配置或 runner 注入。准确但需人工/规则映射中文 topic→英文 query。
2. **从 script_json 的 img_prompt 提取主体词**：web_image_search 内部 parse_script + extract_prompts 拿到 img_prompt 列表，正则抽首个天体名词（jupiter/mars/earth/saturn/nebula/galaxy/black hole/sun/star/aurora/comet/...）。全自动无需额外配置，但正则覆盖度依赖天体词典。

**选 2 为主 + 1 为补充**：web_image_search 默认从 script_json 提取（Graph C 把 `{{script_json}}` 传给 queries，工具内部解）。天体词典常量 `CELESTIAL_TERMS`（jupiter/mars/earth/saturn/neptune/uranus/venus/mercury/sun/moon/nebula/galaxy/black hole/milky way/aurora/comet/asteroid/supernova/quasar/pulsar/...）正则匹配 img_prompt 首个命中词作 query。命中不到 → fallback 用整个 img_prompt 截断前 3 词作 query（NASA 也支持短语搜索，召回稍弱）。

天体词典放 `douyin_config.json` 的 `web_image.celestial_terms` 段，可配置扩展。

## Graph C 接入 (真实图优先)

改 `graphs/douyin_batch_produce.json`，script 后插 web_image_search + gate，真实图优先，不足回退生成图。

**新增节点**：
- `web_img`：调 web_image_search，`queries={{script_json}}`（工具内部提取天体词），`output_mapping: {"image_paths":"web_image_paths"}`。
- `gate_web`：`{"type":"condition","condition_expr":"web_has_enough == true"}`。

**web_has_enough 怎么来**：AgentRuntime 扁平插值不支持表达式，条件需是已有变量。两选一：
- (a) web_image_search 返回额外字段 `enough`（true/false，按产出数 ≥ 分镜数判），Graph output_mapping 映射 → condition_expr `enough == true`。**简洁，工具自判，推荐**。
- (b) runner/GAS 注入分镜数常量比较。复杂，弃。

**改后边**：
```
start→script→web_img→gate_web
gate_web --true--> img 用 web_image_paths (真实图)
gate_web --false--> img_gen (comfyui_image 生成图)
img / img_gen --> tts → compose → enqueue → end
```

**img 节点改两份**：原 `img`（tool_name=comfyui_image）保留作生成图回退分支，新增 `img_web`（type=pass/tool=noop 直接透传 web_image_paths，或复用 comfyui 但跳过生成）。更干净：`img_web` 用一个轻量"透传"——但 AgentRuntime 无 noop 工具。改为：compose 节点直接读 image_paths，gate 前用一个 `select_img` 节点把 web_image_paths 或生成图 paths 写进统一 `image_paths` 变量。

**最终结构（简洁版）**：
```
script → web_img(→web_image_paths) → gate_web(enough==true)
gate_web true  → set_img_web (web_image_paths 写入 image_paths)
gate_web false → img_gen (comfyui_image → image_paths)
set_img_web / img_gen → tts → compose → enqueue → end
```
set_img_web 用 queue_ops 或一个纯赋值工具？AgentRuntime 无纯赋值。**改用**：让 compose 在 gate 两分支前接，gate_web true 分支直接把 web_image_paths 经 output_mapping 复用为 image_paths——但 condition 节点不执行工具不产出 mapping。

**实际可行**（参考 auto_publish dequeue 回环 + queue_publish gate_stock 模式）：condition 节点只判路由不产出。让 gate 两分支各接一个工具节点：
- true → `img_passthrough`：用已有工具把 web_image_paths 设为 image_paths。无纯赋值工具 → **加 graph 层 output_mapping 在 web_img 节点同时映射 image_paths**：`{"image_paths":"image_paths","enough":"web_enough"}`，web_img 一出图就同时写好 image_paths（真实图）+ enough。gate_web false 分支接 `img_gen`(comfyui_image) 覆盖 image_paths 为生成图。true 分支不接生成节点，image_paths 保持真实图直接进 tts。
```
script → web_img (output_mapping: image_paths←image_paths[真实], web_enough←enough)
       → gate_web (web_enough == true)
gate_web true  → tts (image_paths 已是真实图)
gate_web false → img_gen (comfyui_image, output_mapping: image_paths←image_paths[生成]) → tts
tts → compose → enqueue → end
```
**web_img output_mapping 把自身 image_paths 直接映射成全局 image_paths**，true 分支无需额外节点即透传真实图；false 分支 img_gen 生成图覆盖 image_paths。这是最小改动、复用 auto_publish 已验证的"gate 前节点 output_mapping 写好后续变量"模式。

## 版权与合规

NASA Image Library 图属美国联邦政府工作产物，**公共领域无版权**，商用合规。落盘图旁存一份 metadata（nasa_id/title/photographer）到 `out/images/_web_meta_{idx:03d}.json`，备查来源。description 不在视频标注来源（NASA 公共领域无需署名，但留 meta 留痕以备质疑）。

注意：NASA 库含少量非 NASA 来源图（如合作机构带版权），metadata 的 `photographer`/`description` 会标注。下载时若 metadata 含 "copyright" 字段非空 → 跳过该图换下一个 item，只下纯公共领域图。

## 错误处理与回退

| 失败点 | 处理 |
|--------|------|
| NASA search http 非 200 / 网络超时 | warning，该 query 返回空 |
| search 返回 0 items | warning，该 query 返回空 |
| metadata JSON href 取不到 large jpg | warning，该 query 返回空 |
| 下载原图 http 非 200 / 超时 | warning，该 query 返回空 |
| metadata 含 copyright 字段 | 跳过该 item 换下一个 |
| 全部 query 都失败 | 返回 `{"image_paths":[],"enough":false}`，Graph gate_web false → comfyui 生成图 |
| 下载图退化/黑（1024x3 类） | ffmpeg_compose._scene_base 已有坏图兜底（星球占位图），无需 web_image_search 处理 |

web_image_search 永不抛异常阻断 Graph，最差返回空触发上层回退。

## 测试策略

**单测**（mock NASA API，tests/test_douyin_tools.py）：
- `test_web_image_search_parse_queries`：script_json / 列表 / 逗号串 → query 列表。
- `test_web_image_search_extract_celestial`：img_prompt 含 "jupiter great red spot" → query="jupiter"；含 "black hole" → query="black hole"；无天体词 → fallback 前 3 词。
- `test_web_image_search_search_nasa_mock`：monkeypatch _search_nasa 返回固定 items，断言 _resolve_image_url 选 large jpg。
- `test_web_image_search_download_mock`：monkeypatch _download 返回 True，断言 image_paths 数 + enough 字段。
- `test_web_image_search_fallback_empty`：_search_nasa 返回空 → image_paths=[] enough=false。
- `test_web_image_search_skip_copyright`：metadata 含 copyright 字段 → 跳过换下一个 item。

**真实端到端验证**（真实加载 NASA API，非 mock）：
- 真实调 NASA 搜 "jupiter" 下载真实图 → cv2 数值检查非退化（w≥100）非黑（mean≥15 或 std≥3）+ 尺寸合理。
- Graph C 真实跑通真实图优先路径（dry_run，真实图入 compose 合成 mp4）。
- 验证后清理过程数据，只保留最终输出件 + 日志。

## 部署

1. 写 `douyin_tools/web_image_search.py`。
2. `douyin_tools/__init__.py` 注册（registry 18→19）。
3. `douyin_config.json` 加 `web_image` 段（nasa_api_base/timeout/per_query/celestial_terms）。
4. `config.py` 加 `NASA_API_BASE`/`NASA_TIMEOUT`/`WEB_PER_QUERY`/`CELESTIAL_TERMS` 常量（三级回退 env > json > default）。
5. `tests/test_douyin_tools.py` 加 6 单测。
6. `sync_plugins.py` 生成 19 shims（自动，无需改）。
7. `graphs/douyin_batch_produce.json` 加 web_img + gate_web + img_gen 分支。
8. `sync_graphs.py` 重新注册 Graph C 进 daemon store。
9. `python -m pytest tests/ -q` 全绿。
10. launchd restart（env -u FUSION_MLX_API_KEY）。
11. 真实 NASA API 端到端验证。
12. 更新 CLAUDE.md + memory。

## 不做 (out of scope)

- 视频片段搜索/拼接（本轮只图片，用户已确认）。
- Bing/百度搜索引擎抓图（版权风险，用户选官方图库）。
- Wikimedia/ESA 等其他图库（先 NASA，后续扩展留口）。
- 图片后处理裁切调色（NASA 原图直落，ffmpeg_compose 已有等比缩放裁切 + 坏图兜底）。
- 改 ffmpeg_compose（仍只吃 still image_paths）。
- Graph D/auto_publish 改（仅改 Graph C 造片链，发布链不动）。
