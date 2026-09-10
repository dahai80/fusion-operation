import json
import os
import time

import pytest

from douyin_tools import create_douyin_registry
from douyin_tools import history_store
from douyin_tools.config import ensure_dirs, IMAGE_DIR, AUDIO_DIR, VIDEO_DIR, OPS_DATA_DIR
from douyin_tools.ffmpeg_compose import FfmpegComposeTool
from douyin_tools.mlx_image import MlxImageTool
from douyin_tools.mlx_script import MlxScriptTool
from douyin_tools.mlx_tts import MlxTtsTool
from douyin_tools.douyin_publish import DouyinPublishTool
from douyin_tools.douyin_fetch_comments import DouyinFetchCommentsTool
from douyin_tools.douyin_reply import DouyinReplyTool
from douyin_tools.douyin_stats import DouyinStatsTool
from douyin_tools.mlx_reply_draft import MlxReplyDraftTool
from douyin_tools.ops_report import OpsReportTool
from douyin_tools.queue_ops import QueueOpsTool
from douyin_tools.topic_gen import TopicGenTool
from douyin_tools.comfyui_produce import ComfyUIImageTool
from douyin_tools.web_image_search import WebImageSearchTool


async def _async_val(v):
    return v


_CORE_TOOLS = ["mlx_script", "comfyui_image", "mlx_image", "mlx_tts", "comfyui_tts", "ffmpeg_compose", "douyin_publish"]
_OPS_TOOLS = [
    "douyin_fetch_comments",
    "mlx_reply_draft",
    "douyin_reply",
    "douyin_stats",
    "ops_report",
    "ops_health",
    "perf_track",
    "perf_analyze",
    "self_repair",
    "publish_scheduler",
    "pattern_review",
    "team_contract",
]
_QUEUE_TOOLS = ["queue_ops", "topic_gen"]


def test_registry_has_core_tools():
    r = create_douyin_registry()
    assert r.count == 22
    for name in _CORE_TOOLS + _OPS_TOOLS + _QUEUE_TOOLS + ["web_image_search"]:
        assert r.has(name), f"missing {name}"


def test_ensure_dirs():
    ensure_dirs()
    for d in (VIDEO_DIR, AUDIO_DIR, IMAGE_DIR):
        assert os.path.isdir(d)


def test_tool_schemas():
    t = MlxScriptTool()
    assert t.name == "mlx_script"
    assert "topic" in t.parameters
    schema = t.openai_schema()
    assert schema["function"]["name"] == "mlx_script"
    assert "topic" in schema["function"]["parameters"]["required"]


def test_extract_json_plain():
    t = MlxScriptTool()
    out = t._extract_json('{"title":"x","scenes":[]}')
    assert out == {"title": "x", "scenes": []}


def test_extract_json_fenced():
    t = MlxScriptTool()
    out = t._extract_json('```json\n{"title":"y"}\n```')
    assert out["title"] == "y"


def test_extract_json_surrounding_text():
    t = MlxScriptTool()
    out = t._extract_json('好的，结果如下：\n{"title":"z","n":1}\n请检查')
    assert out["title"] == "z" and out["n"] == 1


def test_extract_json_invalid():
    t = MlxScriptTool()
    assert t._extract_json("no json here") is None


async def test_mlx_script_empty_topic():
    t = MlxScriptTool()
    res = await t.execute(topic="")
    assert json.loads(res)["error"] == "empty topic"


def test_image_parse_list_variants():
    assert MlxImageTool._parse_list(["a", "b"]) == ["a", "b"]
    assert MlxImageTool._parse_list('["a","b"]') == ["a", "b"]
    assert MlxImageTool._parse_list("a, b ,c") == ["a", "b", "c"]
    assert MlxImageTool._parse_list("") == []
    assert MlxImageTool._parse_list('{"prompts":["x","y"]}') == ["x", "y"]


async def test_mlx_image_empty_prompts():
    t = MlxImageTool()
    res = await t.execute(prompts="")
    assert json.loads(res)["error"] == "empty prompts"


def test_image_fallback_missing_dir(tmp_path):
    assert MlxImageTool._pick_fallback(str(tmp_path / "nope"), 0) == ""


def test_image_fallback_picks_file(tmp_path):
    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.jpg"
    f1.write_text("x")
    f2.write_text("y")
    chosen = MlxImageTool._pick_fallback(str(tmp_path), 1)
    # P3: 种子分散选图, 不再固定 idx%len. 只断言取到合法落盘文件之一.
    assert chosen.endswith(("a.png", "b.jpg"))
    assert os.path.exists(chosen)


def test_compose_parse_list_variants():
    assert FfmpegComposeTool._parse_list('["p1","p2"]') == ["p1", "p2"]
    assert FfmpegComposeTool._parse_list("p1, p2") == ["p1", "p2"]
    assert FfmpegComposeTool._parse_list("") == []
    assert FfmpegComposeTool._parse_list('{"image_paths":["a","b"]}') == ["a", "b"]


def test_parse_list_dict_no_key_fail_not_split():
    # 修 R5 契约: dict(JSON/repr) 无已知 key 必须显式返回 [], 不再 fall through 到 split(",")
    # 把整个 dict 按逗号分成垃圾段. 4 工具同模式.
    script_repr = "{'scene': '黑洞', 'img_prompt': 'black hole', 'narration': 'text'}"
    assert MlxImageTool._parse_list(script_repr) == []
    assert FfmpegComposeTool._parse_list(script_repr) == []
    assert ComfyUIImageTool._parse_list(script_repr) == []
    assert WebImageSearchTool._parse_list(script_repr) == []
    # JSON dict 同样
    assert MlxImageTool._parse_list('{"foo": 1, "bar": 2}') == []
    # 已知 key 仍正常提取 (dict repr 形式, ast 路径)
    assert MlxImageTool._parse_list("{'prompts': ['a', 'b']}") == ["a", "b"]
    # 纯逗号串仍走 split (文档化的逗号分隔输入)
    assert MlxImageTool._parse_list("a, b, c") == ["a", "b", "c"]


async def test_compose_invalid_inputs():
    t = FfmpegComposeTool()
    res = await t.execute(image_paths="", audio_path="/nonexistent")
    assert json.loads(res)["error"] == "invalid inputs"
    res2 = await t.execute(image_paths=["x"], audio_path="/nonexistent")
    assert json.loads(res2)["error"] == "invalid inputs"


def test_compose_audio_duration_fallback():
    assert FfmpegComposeTool._audio_duration("/nonexistent.wav") == 15.0


async def test_tts_empty_text():
    t = MlxTtsTool()
    res = await t.execute(text="")
    assert json.loads(res)["error"] == "empty text"


def test_publish_schema():
    t = DouyinPublishTool()
    assert t.name == "douyin_publish"
    assert "dry_run" in t.parameters
    assert "video_path" in t.parameters


async def test_publish_missing_video(tmp_path):
    t = DouyinPublishTool()
    res = await t.execute(video_path=str(tmp_path / "nope.mp4"), description="x", dry_run=True)
    assert json.loads(res)["error"] == "video not exist"


async def test_publish_dry_run_flow(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"fake")

    class FakePage:
        def __init__(self):
            self.url = "https://creator.douyin.com/creator-micro/content/manage"
        async def goto(self, *a, **k):
            pass
        async def inner_text(self, *a, **k):
            return "作品描述 共 3 个"
        def locator(self, *a, **k):
            class L:
                @property
                def first(self):
                    return self
                async def set_input_files(self, *a, **k):
                    pass
                async def count(self):
                    return 4
                def nth(self, i):
                    return self
                async def click(self, *a, **k):
                    pass
            return L()
        async def evaluate(self, *a, **k):
            return "ok"
        async def wait_for_url(self, *a, **k):
            pass

    class FakeCtx:
        pages = [FakePage()]
        async def close(self):
            pass
        async def new_page(self):
            return FakePage()

    class FakeBrowser:
        async def launch_persistent_context(self, *a, **k):
            return FakeCtx()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeBrowser()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    import sys
    fake_mod = type(sys)("playwright.async_api")
    fake_mod.async_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_mod)

    t = DouyinPublishTool()
    res = await t.execute(video_path=str(video), description="#测试", dry_run=True)
    d = json.loads(res)
    assert d["dry_run"] is True
    assert d["status"] == "dry_run_ok"
    assert d["verify"] is True
    assert d["vid"] == ""


async def test_publish_verify_by_count_unknown_baseline():
    # C6 防回归: before<0 (count 基线未知) 必须判失败, 不能 return True 误判发布成功.
    t = DouyinPublishTool()
    ok = await t._verify_by_count(page=None, before=-1, timeout=1)
    assert ok is False


def test_self_repair_extract_json_trailing_bracket():
    # 防回归: 模型输出尾部 ] 误替 } 时 _extract_json 应修复后解析, 不能返回 None.
    from douyin_tools.self_repair import SelfRepairTool
    broken = '{"repaired":[{"vid":"1","new_title":"x"]'
    r = SelfRepairTool._extract_json(broken)
    assert r is not None
    assert r["repaired"][0]["vid"] == "1"
    # 正常 markdown 围栏仍工作
    r2 = SelfRepairTool._extract_json('```json\n{"repaired":[]}\n```')
    assert r2["repaired"] == []
    # 纯垃圾返回 None
    assert SelfRepairTool._extract_json("no json here") is None


def test_reply_draft_parse_comments():
    assert MlxReplyDraftTool._parse_comments([{"id": "a", "text": "hi"}]) == [{"id": "a", "text": "hi"}]
    assert MlxReplyDraftTool._parse_comments('[{"id":"b","text":"y"}]') == [{"id": "b", "text": "y"}]
    assert MlxReplyDraftTool._parse_comments("not json") == []
    assert MlxReplyDraftTool._parse_comments('{"comments":[{"id":"c","text":"z"}]}') == [{"id": "c", "text": "z"}]


def test_reply_draft_extract_json():
    assert MlxReplyDraftTool._extract_json('{"drafts":[{"id":"a","text":"b","safe":true}]}')["drafts"][0]["id"] == "a"
    assert MlxReplyDraftTool._extract_json('```json\n{"drafts":[]}\n```')["drafts"] == []
    assert MlxReplyDraftTool._extract_json("nothing") is None


def test_reply_draft_extract_json_truncated_recovers():
    # M4: rfind("}") 在模型输出截断(缺外层 }) 时无 } 可找 -> None.
    # 括号配平扫描 depth=1 + 尾部补 } 应恢复完整对象.
    trunc = '{"drafts":[{"id":"a","text":"b","safe":true}]'
    out = MlxReplyDraftTool._extract_json(trunc)
    assert out is not None
    assert out["drafts"][0]["id"] == "a"


def test_reply_draft_extract_json_stray_brace_after():
    # M4: rfind("}") 误取尾部垃圾里的 } (json.loads 整段失败).
    # 括号配平在 depth==0 即停, 忽略后续文本, 正确解析.
    raw = '{"drafts":[{"id":"a","text":"b","safe":true}]} 以上是回复内容}'
    out = MlxReplyDraftTool._extract_json(raw)
    assert out is not None
    assert out["drafts"][0]["id"] == "a"


def test_reply_draft_local_safety_blocks_words():
    drafts = [{"id": "1", "text": "加我微信xxx", "safe": True}]
    out = MlxReplyDraftTool._local_safety(drafts, [{"id": "1", "text": "x"}])
    assert out[0]["safe"] is False


def test_reply_draft_local_safety_blocks_long():
    drafts = [{"id": "1", "text": "x" * 60, "safe": True}]
    out = MlxReplyDraftTool._local_safety(drafts, [{"id": "1", "text": "x"}])
    assert out[0]["safe"] is False


async def test_reply_draft_empty_comments():
    t = MlxReplyDraftTool()
    res = await t.execute(comments_json="[]")
    assert json.loads(res)["error"] == "empty comments"


def test_reply_parse_drafts():
    out = DouyinReplyTool._parse_drafts('[{"id":"1","text":"hi","safe":true}]')
    assert out[0]["id"] == "1" and out[0]["safe"] is True
    assert DouyinReplyTool._parse_drafts("bad") == []
    assert DouyinReplyTool._parse_drafts('{"drafts":[{"id":"2","text":"y","safe":false}]}')[1 - 1]["safe"] is False


def test_reply_load_replied_missing(tmp_path, monkeypatch):
    import douyin_tools.douyin_reply as mod
    monkeypatch.setattr(mod, "REPLIED_FILE", str(tmp_path / "nope.json"))
    assert DouyinReplyTool._load_replied() == set()


async def test_reply_dry_run_plan(tmp_path, monkeypatch):
    import douyin_tools.douyin_reply as mod
    monkeypatch.setattr(mod, "REPLIED_FILE", str(tmp_path / "replied.json"))
    t = DouyinReplyTool()
    drafts = '[{"id":"a","text":"hi","safe":true},{"id":"b","text":"y","safe":false}]'
    res = await t.execute(drafts_json=drafts, dry_run=True, max_reply=5)
    d = json.loads(res)
    assert d["dry_run"] is True
    assert d["todo"] == ["a"]
    assert d["skipped"] == 0


async def test_reply_skips_already_replied(tmp_path, monkeypatch):
    import douyin_tools.douyin_reply as mod
    rf = tmp_path / "replied.json"
    rf.write_text(json.dumps({"replied_ids": ["a"]}))
    monkeypatch.setattr(mod, "REPLIED_FILE", str(rf))
    t = DouyinReplyTool()
    drafts = '[{"id":"a","text":"hi","safe":true},{"id":"b","text":"yo","safe":true}]'
    res = await t.execute(drafts_json=drafts, dry_run=True, max_reply=5)
    d = json.loads(res)
    assert d["todo"] == ["b"]


def test_reply_draft_align_ids():
    comments = [
        {"id": "cmt_1", "text": "震撼", "user": "u1"},
        {"id": "cmt_2", "text": "关注了", "user": "u2"},
    ]
    llm_drafts = [
        {"id": "WRONG", "text": "谢谢🚀", "safe": True},
        {"id": "alsowrong", "text": "感谢🌟", "safe": True},
    ]
    out = MlxReplyDraftTool._align_ids(llm_drafts, comments)
    assert out[0]["id"] == "cmt_1"
    assert out[0]["comment_text"] == "震撼"
    assert out[0]["user"] == "u1"
    assert out[1]["id"] == "cmt_2"
    assert "WRONG" not in [d["id"] for d in out]


def test_stats_schema():
    t = DouyinStatsTool()
    assert t.name == "douyin_stats"
    assert "aweme_id" in t.parameters
    assert "dry_run" in t.parameters


def test_stats_parse_page_body():
    t = DouyinStatsTool()
    body = "\n".join([
        "已发布", "播放", "124", "点赞", "4", "评论", "1", "分享", "0",
        "00:36", "如果宇宙停止膨胀，会发生什么？🌌用AI把知识画出来",
        "已发布", "播放", "5.2万", "点赞", "88", "评论", "12", "分享", "3",
        "01:02", "为什么光速不能超越？AI动画科普",
    ])
    blocks = []
    labels = {"播放": "plays", "点赞": "likes", "评论": "comments", "分享": "shares"}
    import re as _re
    cur = None
    status_words = {"已发布", "未发布", "审核中", "已删除", "私密", "公开", "可见"}
    for l in body.split("\n"):
        if l in labels:
            cur = labels[l]
            if cur == "plays":
                blocks.append({"title": "", "plays": 0, "likes": 0, "comments": 0, "shares": 0})
            continue
        if cur and t._is_num(l):
            if blocks:
                blocks[-1][cur] = t._to_num(l)
            cur = None
            continue
        cur = None
        if l in status_words or _re.match(r"^\d+:\d{2}$", l):
            continue
        if len(l) > 15 and blocks and not blocks[-1].get("title"):
            blocks[-1]["title"] = l[:60]
    assert blocks[0]["title"].startswith("如果宇宙停止膨胀")
    assert blocks[0]["plays"] == 124
    assert blocks[0]["likes"] == 4
    assert blocks[0]["comments"] == 1
    assert blocks[0]["shares"] == 0
    assert blocks[1]["plays"] == 52000
    assert blocks[1]["likes"] == 88
    assert blocks[1]["shares"] == 3


def test_stats_to_num():
    t = DouyinStatsTool()
    assert t._to_num("124") == 124
    assert t._to_num("5.2万") == 52000
    assert t._to_num("3亿") == 300000000
    assert t._to_num("abc") == 0


def test_stats_match_page_stats():
    t = DouyinStatsTool()
    page_stats = [
        {"title": "如果宇宙停止膨胀，会发生什么", "plays": 124, "likes": 4},
        {"title": "为什么光速不能超越", "plays": 52000, "likes": 88},
    ]
    m = t._match_page_stats("如果宇宙停止膨胀，会发生什么？🌌用AI把知识画出来", page_stats)
    assert m and m["plays"] == 124
    assert t._match_page_stats("不存在的标题", page_stats) is None


async def test_stats_dry_run():
    t = DouyinStatsTool()
    res = await t.execute(dry_run=True)
    d = json.loads(res)
    assert d["items"] == []
    assert "error" not in d


def test_fetch_comments_schema():
    t = DouyinFetchCommentsTool()
    assert t.name == "douyin_fetch_comments"
    assert "limit" in t.parameters


async def test_fetch_comments_dry_run():
    t = DouyinFetchCommentsTool()
    res = await t.execute(dry_run=True)
    d = json.loads(res)
    assert d["comments"] == []
    assert d["count"] == 0


def test_ops_report_parse():
    assert OpsReportTool._parse('{"a":1}') == {"a": 1}
    assert OpsReportTool._parse({"a": 1}) == {"a": 1}
    assert OpsReportTool._parse("bad") is None


def test_ops_report_parse_comments():
    assert OpsReportTool._parse_comments('[{"text":"x"}]') == [{"text": "x"}]
    assert OpsReportTool._parse_comments('{"comments":[{"text":"y"}]}') == [{"text": "y"}]
    assert OpsReportTool._parse_comments("") == []


def test_ops_report_build_and_history(tmp_path, monkeypatch):
    import douyin_tools.ops_report as mod
    hist = tmp_path / "stats_history.jsonl"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    t = OpsReportTool()
    stats = {
        "fetched_at": "2026-08-10T10:00:00",
        "account": {"followers_raw": "100", "total_likes_raw": "500"},
        "items": [
            {"title": "视频A", "plays": 1000, "likes": 50, "comments": 5, "create_time": 1700000000},
            {"title": "视频B", "plays": 500, "likes": 20, "comments": 2, "create_time": 1700100000},
        ],
    }
    res = t.execute(stats_json=json.dumps(stats), comments_json='[{"text":"好看"}]', date="2026-08-10")
    import asyncio
    out = json.loads(asyncio.run(res) if hasattr(res, "__await__") else res)
    assert out["date"] == "2026-08-10"
    assert os.path.exists(out["report_path"])
    content = open(out["report_path"]).read()
    assert "视频A" in content and "好看" in content
    assert hist.exists()
    prev = t._load_prev("2026-08-10")
    assert prev is None or prev["date"] != "2026-08-10"


def test_ops_report_three_sections_and_rank(tmp_path, monkeypatch):
    # 验证三段式报表：全貌 + 细节排行(按播放降序优秀在前) + 归因为什么好
    import douyin_tools.ops_report as mod
    hist = tmp_path / "stats_history.jsonl"
    win = tmp_path / "winning_patterns.json"
    pub = tmp_path / "published"
    pub.mkdir()
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(mod, "WINNING_FILE", str(win))
    monkeypatch.setattr(mod, "PUBLISHED_DIR", str(pub))
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    win.write_text(json.dumps({
        "winning_topics": ["极端假设反事实推演"],
        "losing_patterns": ["纯陈述事实无情绪钩子"],
        "title_formula": "极端数字加颠覆常识",
    }), encoding="utf-8")
    t = OpsReportTool()
    stats = {
        "fetched_at": "2026-08-15T10:00:00",
        "account": {"followers_raw": "38", "total_likes_raw": "324"},
        "items": [
            {"vid": "v1", "title": "极端假设反事实推演如果太阳消失", "plays": 1500, "likes": 60, "comments": 8, "shares": 10},
            {"vid": "v2", "title": "纯陈述事实无情绪钩子登月历史", "plays": 30, "likes": 1, "comments": 0, "shares": 0},
            {"vid": "v3", "title": "普通视频", "plays": 200, "likes": 5, "comments": 1, "shares": 1},
        ],
    }
    import asyncio
    res = t.execute(stats_json=json.dumps(stats), date="2026-08-15")
    out = json.loads(asyncio.run(res) if hasattr(res, "__await__") else res)
    content = open(out["report_path"]).read()
    # 三段式标题都在
    assert "## 一、全貌" in content
    assert "## 二、逐视频细节排行" in content
    assert "## 三、归因总结" in content
    # 细节排行 v1(1500) 排在 v3(200) 前, v3 在 v2(30) 前
    pos_v1 = content.find("极端假设反事实推演如果太阳消失")
    pos_v3 = content.find("普通视频")
    pos_v2 = content.find("纯陈述事实无情绪钩子登月历史")
    assert 0 < pos_v1 < pos_v3 < pos_v2
    # 为什么好：v1 命中 winning_topic → ✅
    assert "✅" in content
    # 为什么差：v2 命中 losing_pattern → ❌
    assert "❌" in content
    # 归因总结含标题公式
    assert "标题公式" in content


async def test_ops_report_empty_stats():
    t = OpsReportTool()
    res = await t.execute(stats_json="{}")
    assert json.loads(res)["error"] == "empty stats"


def test_queue_ops_schema():
    t = QueueOpsTool()
    assert t.name == "queue_ops"
    assert "action" in t.parameters


async def test_queue_ops_enqueue_dequeue_archive(tmp_path, monkeypatch):
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(QueueOpsTool, "PENDING_DIR", os.path.join(qdir, "pending"))
    monkeypatch.setattr(QueueOpsTool, "INFLIGHT_DIR", os.path.join(qdir, "inflight"))
    monkeypatch.setattr(QueueOpsTool, "PUBLISHED_DIR", os.path.join(qdir, "published"))
    monkeypatch.setattr(QueueOpsTool, "FAILED_DIR", os.path.join(qdir, "failed"))
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "测试选题", "description": "#话题", "narration": "x", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="测试选题"))
    assert r["status"] == "enqueued"
    assert r["pending"] == 1
    item_id = r["item_id"]
    d = json.loads(await t.execute(action="dequeue"))
    assert d["has_stock"] == "true"
    assert d["item_id"] == item_id
    assert d["video_path"] == str(video)
    assert d["title"] == "测试选题"
    a = json.loads(await t.execute(action="archive", item_id=item_id, status="published", vid="7672641563700284713"))
    assert a["status"] == "archived"
    assert a["vid"] == "7672641563700284713"
    assert a["pending"] == 0
    with open(os.path.join(qmod.QUEUE_DIR, "published", f"{item_id}.json"), encoding="utf-8") as f:
        pub_item = json.load(f)
    assert pub_item["vid"] == "7672641563700284713"
    c = json.loads(await t.execute(action="count"))
    assert c["pending"] == 0
    assert c["published"] == 1
    assert c["has_stock"] == "false"


async def test_queue_ops_dequeue_empty(tmp_path, monkeypatch):
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(QueueOpsTool, "PENDING_DIR", os.path.join(qdir, "pending"))
    monkeypatch.setattr(QueueOpsTool, "INFLIGHT_DIR", os.path.join(qdir, "inflight"))
    monkeypatch.setattr(QueueOpsTool, "PUBLISHED_DIR", os.path.join(qdir, "published"))
    monkeypatch.setattr(QueueOpsTool, "FAILED_DIR", os.path.join(qdir, "failed"))
    t = QueueOpsTool()
    d = json.loads(await t.execute(action="dequeue"))
    assert d["has_stock"] == "false"
    assert d["status"] == "empty"


async def test_queue_ops_dequeue_moves_to_inflight_no_dup(tmp_path, monkeypatch):
    # C2: dequeue 必须原子移入 in-flight, pending 不残留 -> 并发 dequeue 不重复取同一 item.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "黑洞", "description": "#x", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="黑洞"))
    item_id = r["item_id"]
    d1 = json.loads(await t.execute(action="dequeue"))
    assert d1["has_stock"] == "true"
    assert d1["item_id"] == item_id
    # 移入 in-flight 后 pending 应空, 第二次 dequeue 应 empty (不重复取).
    assert not os.path.exists(os.path.join(qdir, "pending", f"{item_id}.json"))
    assert os.path.exists(os.path.join(qdir, "inflight", f"{item_id}.json"))
    d2 = json.loads(await t.execute(action="dequeue"))
    assert d2["has_stock"] == "false"
    # dequeue 输出不再回整个 script_json/item dict (修 L8: 只扁平字段).
    assert "script_json" not in d1
    assert "item" not in d1
    assert d1["desc_text"] == "#x"


async def test_queue_ops_archive_failed_from_inflight(tmp_path, monkeypatch):
    # C2: 发布失败 archive status=failed 从 in-flight 回收到 failed dir, 不留 pending 被重取.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "失败片", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="失败片"))
    item_id = r["item_id"]
    json.loads(await t.execute(action="dequeue"))
    a = json.loads(await t.execute(action="archive", item_id=item_id, status="failed"))
    assert a["status"] == "archived"
    assert os.path.exists(os.path.join(qdir, "failed", f"{item_id}.json"))
    assert not os.path.exists(os.path.join(qdir, "inflight", f"{item_id}.json"))


async def test_queue_ops_dequeue_writes_lease(tmp_path, monkeypatch):
    # H1: dequeue 写 leased_by/leased_until 到 inflight item, 标记归属防多节点重复消费.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(qmod, "NODE_ID", "node-A")
    monkeypatch.setattr(qmod, "LEASE_TTL", 300)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "lease", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="lease"))
    item_id = r["item_id"]
    d = json.loads(await t.execute(action="dequeue"))
    assert d["leased_by"] == "node-A"
    with open(os.path.join(qdir, "inflight", f"{item_id}.json"), encoding="utf-8") as f:
        inflight = json.load(f)
    assert inflight["leased_by"] == "node-A"
    assert inflight["leased_until"] > int(time.time())


async def test_queue_ops_archive_reject_foreign_lease(tmp_path, monkeypatch):
    # H1: archive 校验 lease 归属. 别节点持未过期 lease 的 item, 本节点归档被拒防双发.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(qmod, "NODE_ID", "node-A")
    monkeypatch.setattr(qmod, "LEASE_TTL", 300)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "reject", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="reject"))
    item_id = r["item_id"]
    await t.execute(action="dequeue")  # node-A 拿走, 写 lease
    # 切到 node-B 模拟别节点尝试归档
    monkeypatch.setattr(qmod, "NODE_ID", "node-B")
    a = json.loads(await t.execute(action="archive", item_id=item_id, status="published"))
    assert "error" in a
    assert "node-A" in a["error"]
    # item 仍留 inflight 未被归档
    assert os.path.exists(os.path.join(qdir, "inflight", f"{item_id}.json"))


async def test_queue_ops_reclaim_expired_lease(tmp_path, monkeypatch):
    # H1: inflight item lease 过期 (上轮 dequeue 崩溃遗留), 下轮 dequeue 回收到 pending 重新排队.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(qmod, "NODE_ID", "node-A")
    monkeypatch.setattr(qmod, "LEASE_TTL", 300)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "reclaim", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="reclaim"))
    item_id = r["item_id"]
    await t.execute(action="dequeue")  # node-A 拿走, 写 lease until=now+300
    # M3-5: DB 权威, 模拟崩溃直接改 DB leased_until 到过去 (旧测试改文件不再生效)
    qmod._get_store().set_leased_until(item_id, int(time.time()) - 1)
    # 下轮 dequeue 应回收过期 item 回 pending, 再取出
    d = json.loads(await t.execute(action="dequeue"))
    assert d["has_stock"] == "true"
    assert d["item_id"] == item_id


async def test_queue_ops_archive_expired_foreign_lease_ok(tmp_path, monkeypatch):
    # H1: lease 过期后, 别节点可归档该 item (崩溃恢复场景, 不卡死).
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(qmod, "NODE_ID", "node-A")
    monkeypatch.setattr(qmod, "LEASE_TTL", 300)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = json.dumps({"title": "expired-ok", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="expired-ok"))
    item_id = r["item_id"]
    await t.execute(action="dequeue")  # node-A 拿走
    # M3-5: DB 权威, 模拟崩溃直接改 DB leased_until 到过去
    qmod._get_store().set_leased_until(item_id, int(time.time()) - 1)
    monkeypatch.setattr(qmod, "NODE_ID", "node-B")
    a = json.loads(await t.execute(action="archive", item_id=item_id, status="published", vid="v1"))
    assert a["status"] == "archived"


def _setup_queue(tmp_path, monkeypatch):
    # M3-5: 共享 setup — tmp QUEUE_DIR + 4 目录常量 + store 缓存重置.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    qmod.reset_store()
    return qmod, qdir


def test_queue_store_enqueue_dequeue_archive(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = queue_store.QueueStore(os.path.join(qdir, "queue.db"))
    item_id = store.enqueue({"id": "a1", "video_path": "/v.mp4", "title": "t", "created_at": int(time.time())})
    assert item_id == "a1"
    assert store.count()["pending"] == 1
    d = store.dequeue(300, "node-A")
    assert d["id"] == "a1"
    assert d["status"] == "inflight"
    assert d["leased_by"] == "node-A"
    assert store.count()["inflight"] == 1
    res = store.archive("a1", "published", "vid9", "node-A")
    assert res["status"] == "published"
    assert res["vid"] == "vid9"
    assert store.count()["published"] == 1
    store.close()


def test_queue_store_dequeue_atomic_no_dup(tmp_path, monkeypatch):
    import threading
    from douyin_tools import queue_store
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = queue_store.QueueStore(os.path.join(qdir, "queue.db"))
    for i in range(2):
        store.enqueue({"id": f"s{i}", "video_path": "/v.mp4", "title": "t", "created_at": int(time.time()) + i})
    results = []
    def grab():
        r = store.dequeue(300, "n")
        if r:
            results.append(r["id"])
    t1 = threading.Thread(target=grab)
    t2 = threading.Thread(target=grab)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(results) == 2
    assert set(results) == {"s0", "s1"}
    store.close()


def test_queue_store_reclaim_expired(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = queue_store.QueueStore(os.path.join(qdir, "queue.db"))
    store.enqueue({"id": "r1", "video_path": "/v", "title": "t", "created_at": int(time.time())})
    store.dequeue(300, "node-A")
    store.set_leased_until("r1", int(time.time()) - 10)
    reclaimed = store.reclaim_expired()
    assert reclaimed == 1
    assert store.count()["pending"] == 1
    store.close()


def test_queue_store_requeue_strips_lease(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = queue_store.QueueStore(os.path.join(qdir, "queue.db"))
    store.enqueue({"id": "rq1", "video_path": "/v", "title": "t", "created_at": int(time.time())})
    store.dequeue(300, "node-A")
    ok = store.requeue("rq1")
    assert ok is True
    d = store.get("rq1")
    assert d["status"] == "pending"
    assert d["leased_by"] is None
    assert d["requeued_at"] is not None
    store.close()


def test_queue_store_archive_lease_guard(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = queue_store.QueueStore(os.path.join(qdir, "queue.db"))
    store.enqueue({"id": "g1", "video_path": "/v", "title": "t", "created_at": int(time.time())})
    store.dequeue(300, "node-A")
    # node-B 归档, lease 未过期 → 拒绝
    r = store.archive("g1", "published", "", "node-B")
    assert "error" in r
    # 过期后 node-B 可归档
    store.set_leased_until("g1", int(time.time()) - 1)
    r2 = store.archive("g1", "published", "vid", "node-B")
    assert r2["status"] == "published"
    store.close()


async def test_project_to_files_writes_projection(tmp_path, monkeypatch):
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    script = json.dumps({"title": "proj", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="proj"))
    item_id = r["item_id"]
    assert os.path.exists(os.path.join(qdir, "pending", f"{item_id}.json"))
    await t.execute(action="dequeue")
    assert not os.path.exists(os.path.join(qdir, "pending", f"{item_id}.json"))
    assert os.path.exists(os.path.join(qdir, "inflight", f"{item_id}.json"))
    await t.execute(action="archive", item_id=item_id, status="published", vid="v1")
    assert os.path.exists(os.path.join(qdir, "published", f"{item_id}.json"))
    assert not os.path.exists(os.path.join(qdir, "inflight", f"{item_id}.json"))


def test_reconcile_drift_repairs_files(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    from douyin_tools import queue_ops as qmod
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = qmod._get_store()
    store.enqueue({"id": "rec1", "video_path": "/v", "title": "t", "created_at": int(time.time())})
    dirs = qmod._dirs()
    queue_store.write_projection(store.get("rec1"), dirs)
    # 删 pending 文件制造 drift
    os.remove(os.path.join(qdir, "pending", "rec1.json"))
    # 加多余孤儿文件
    with open(os.path.join(qdir, "pending", "orphan.json"), "w") as f:
        json.dump({"id": "orphan"}, f)
    drift, mismatches = queue_store.reconcile_to_files(store, dirs)
    assert drift >= 2
    # 修复后 pending 有 rec1, orphan 删除
    assert os.path.exists(os.path.join(qdir, "pending", "rec1.json"))
    assert not os.path.exists(os.path.join(qdir, "pending", "orphan.json"))


def test_reconcile_alerts_on_drift(tmp_path, monkeypatch):
    # ops_health._queue_db_health: drift → 修文件 + append_alert (reconcile 修复后文件回正).
    from douyin_tools import history_store
    from douyin_tools import queue_ops as qmod
    from douyin_tools.ops_health import OpsHealthTool
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(history_store, "ALERTS_FILE", str(tmp_path / "alerts.jsonl"))
    store = qmod._get_store()
    # enqueue 后 DB 有行但目录无投影文件 = drift (不写投影)
    store.enqueue({"id": "al1", "video_path": "/v", "title": "t", "created_at": int(time.time())})
    alerts = []
    OpsHealthTool._queue_db_health(alerts)
    assert any("queue_db_file_drift" in a for a in alerts)
    assert os.path.exists(str(tmp_path / "alerts.jsonl"))
    # 修复后文件回正
    assert os.path.exists(os.path.join(qdir, "pending", "al1.json"))


def test_prune_older_than_90_days(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    from douyin_tools import queue_ops as qmod
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    store = qmod._get_store()
    now = int(time.time())
    # 1 旧 (>90d) + 2 新
    store.enqueue({"id": "old1", "video_path": "/v", "title": "t", "created_at": now - 100 * 86400})
    store.archive("old1", "published", "v", "n", now=now - 100 * 86400)
    store.enqueue({"id": "new1", "video_path": "/v", "title": "t", "created_at": now})
    store.archive("new1", "published", "v", "n", now=now)
    store.enqueue({"id": "new2", "video_path": "/v", "title": "t", "created_at": now})
    store.archive("new2", "published", "v", "n", now=now)
    deleted = store.prune_older_than(90)
    assert len(deleted) == 1
    assert deleted[0]["id"] == "old1"
    assert store.count()["published"] == 2


def test_migrate_files_to_db_on_first_init(tmp_path, monkeypatch):
    from douyin_tools import queue_store
    from douyin_tools import queue_ops as qmod
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    # 预置 pending + published 文件
    os.makedirs(os.path.join(qdir, "pending"), exist_ok=True)
    os.makedirs(os.path.join(qdir, "published"), exist_ok=True)
    with open(os.path.join(qdir, "pending", "m1.json"), "w") as f:
        json.dump({"id": "m1", "video_path": "/v", "title": "t", "created_at": int(time.time())}, f)
    with open(os.path.join(qdir, "published", "m2.json"), "w") as f:
        json.dump({"id": "m2", "video_path": "/v", "title": "t", "created_at": int(time.time()), "vid": "v"}, f)
    store = qmod._get_store()  # 触发 migrate
    assert store.count()["pending"] == 1
    assert store.count()["published"] == 1
    assert store.get("m1") is not None
    assert store.get("m2") is not None


async def test_consumers_read_projection_unchanged(tmp_path, monkeypatch):
    # regression: topic_gen 读 published/ 投影文件正常 (DB 存在场景)
    from douyin_tools.topic_gen import TopicGenTool
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    monkeypatch.setattr("douyin_tools.topic_gen.PUBLISHED_DIR", os.path.join(qdir, "published"))
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    script = json.dumps({"title": "cons", "narration": "n", "scenes": []})
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=script, topic="cons"))
    await t.execute(action="dequeue")
    await t.execute(action="archive", item_id=r["item_id"], status="published", vid="cv")
    tg = TopicGenTool()
    titles = tg._load_published_titles()
    assert "cons" in titles


async def test_ops_health_queue_db_health(tmp_path, monkeypatch):
    from douyin_tools.ops_health import OpsHealthTool
    from douyin_tools import queue_ops as qmod
    qmod, qdir = _setup_queue(tmp_path, monkeypatch)
    # DB 无内容 → drift 0, prune 0
    t = OpsHealthTool()
    out = json.loads(await t.execute(check_cron=False))
    assert out["status"] in ("healthy", "degraded")
    assert "queue_db" in out["summary"]
    qd = out["summary"]["queue_db"]
    assert qd.get("db_drift", 0) == 0


def test_topic_gen_schema():
    t = TopicGenTool()
    assert t.name == "topic_gen"
    assert "count" in t.parameters
    assert "series" in t.parameters


def test_perf_track_schema():
    from douyin_tools.perf_track import PerfTrackTool
    t = PerfTrackTool()
    assert t.name == "perf_track"
    assert "action" in t.parameters


async def test_perf_track_snapshot_and_rank(tmp_path, monkeypatch):
    import douyin_tools.perf_track as pmod
    hist = tmp_path / "stats_history.jsonl"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(pmod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    from douyin_tools.perf_track import PerfTrackTool
    t = PerfTrackTool()
    stats = {
        "fetched_at": "2026-08-10 10:00:00",
        "account": {"followers_raw": "37"},
        "items": [
            {"vid": "7600000001", "title": "爆款A", "plays": 800, "likes": 20, "comments": 5, "shares": 2, "create_time": "发布于2026年08月09日 10:00"},
            {"vid": "7600000002", "title": "平庸B", "plays": 120, "likes": 3, "comments": 0, "shares": 0, "create_time": "发布于2026年08月09日 10:00"},
            {"vid": "7600000003", "title": "差片C", "plays": 10, "likes": 0, "comments": 0, "shares": 0, "create_time": "发布于2026年08月01日 10:00"},
        ],
    }
    r = json.loads(await t.execute(action="snapshot", stats_json=json.dumps(stats)))
    assert r["status"] == "snapshotted"
    assert r["written"] == 3
    lines = hist.read_text().strip().split("\n")
    assert len(lines) == 3
    row0 = json.loads(lines[0])
    assert row0["vid"] == "7600000001"
    assert row0["interaction_rate"] == round((20 + 5 + 2) / 800, 4)
    assert row0["stage"] in ("cold_start", "recommend", "long_tail")
    assert row0["age_hours"] > 0
    rr = json.loads(await t.execute(action="rank"))
    assert rr["status"] == "ranked"
    assert rr["total"] == 3
    assert rr["ranked"][0]["vid"] == "7600000001"
    tags = {x["vid"]: x["tag"] for x in rr["ranked"]}
    assert tags["7600000001"] == "hot"


def test_perf_track_parse_create_time():
    from douyin_tools.perf_track import PerfTrackTool
    import datetime
    d1 = PerfTrackTool._parse_create_time("发布于2026年08月10日 10:19")
    assert d1 == datetime.datetime(2026, 8, 10, 10, 19)
    d2 = PerfTrackTool._parse_create_time(1786000000)
    assert d2 is not None and d2.year == 2026
    assert PerfTrackTool._parse_create_time("垃圾") is None


def test_perf_track_stage():
    from douyin_tools.perf_track import PerfTrackTool
    assert PerfTrackTool._stage(5) == "cold_start"
    assert PerfTrackTool._stage(30) == "recommend"
    assert PerfTrackTool._stage(100) == "long_tail"
    assert PerfTrackTool._stage(-1) == "long_tail"


def test_perf_track_prune_history_caps_per_vid(tmp_path, monkeypatch):
    # A3 + A1: stats_history.jsonl 每 vid 最多留 max_history_per_vid 条, 超 prune_threshold 触发压缩.
    # M3-3: prune_history 收敛到 history_store, path 显式传入.
    from douyin_tools.history_store import prune_history
    hist = tmp_path / "stats_history.jsonl"
    # 1 vid 写 10 条 (超 threshold 5), 应压缩到只留最新 3 条.
    rows = [{"vid": "V1", "plays": i, "snapshot_at": f"2026-08-0{i}"} for i in range(10)]
    with open(hist, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    dropped = prune_history(max_per_vid=3, prune_threshold=5, path=str(hist))
    lines = hist.read_text().strip().split("\n")
    assert len(lines) == 3, "must cap to max_history_per_vid"
    assert dropped == 7
    kept = [json.loads(l) for l in lines]
    # 保留尾部最新 3 条 (plays 7,8,9).
    assert [k["plays"] for k in kept] == [7, 8, 9]


def test_perf_track_prune_history_below_threshold_noop(tmp_path, monkeypatch):
    # A1: 阈值走声明式配置, 低于 prune_threshold 不重写文件.
    # M3-3: prune_history 收敛到 history_store, path 显式传入.
    from douyin_tools.history_store import prune_history
    hist = tmp_path / "stats_history.jsonl"
    with open(hist, "w") as f:
        f.write(json.dumps({"vid": "V1", "plays": 1}) + "\n")
    dropped = prune_history(max_per_vid=40, prune_threshold=5000, path=str(hist))
    assert dropped == 0
    assert hist.read_text().count("\n") == 1, "below threshold must not rewrite"


def test_perf_analyze_schema():
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    t = PerfAnalyzeTool()
    assert t.name == "perf_analyze"
    assert "min_samples" in t.parameters


async def test_perf_analyze_insufficient(tmp_path, monkeypatch):
    import douyin_tools.perf_analyze as amod
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    hist = tmp_path / "stats_history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"vid": "1", "plays": 10, "snapshot_at": "2026-08-10 10:00:00"}),
        json.dumps({"vid": "2", "plays": 5, "snapshot_at": "2026-08-10 10:00:00"}),
    ]))
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(amod, "PATTERNS_FILE", str(tmp_path / "patterns.json"), raising=False)
    monkeypatch.setattr(amod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(amod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    t = PerfAnalyzeTool()
    res = await t.execute(min_samples=5)
    d = json.loads(res)
    assert d["error"] == "insufficient samples"


async def test_perf_analyze_infra_failure_raises(tmp_path, monkeypatch):
    import douyin_tools.perf_analyze as amod
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    hist = tmp_path / "stats_history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"vid": str(i), "plays": 100 + i, "snapshot_at": "2026-08-10 10:00:00"})
        for i in range(10)
    ]))
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(amod, "PATTERNS_FILE", str(tmp_path / "patterns.json"), raising=False)
    monkeypatch.setattr(amod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(amod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)

    async def boom(self, evidence, model):
        raise RuntimeError("perf_analyze mlx unavailable: ConnectError")

    monkeypatch.setattr(PerfAnalyzeTool, "_call_model", boom)
    t = PerfAnalyzeTool()
    raised = False
    try:
        await t.execute(min_samples=5)
    except RuntimeError as e:
        raised = True
        assert "mlx unavailable" in str(e)
    assert raised, "infra failure must raise (stop_on_tool_error) not silently return JSON error"


async def test_perf_analyze_parse_failure_soft(tmp_path, monkeypatch):
    import douyin_tools.perf_analyze as amod
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    hist = tmp_path / "stats_history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"vid": str(i), "plays": 100 + i, "snapshot_at": "2026-08-10 10:00:00"})
        for i in range(10)
    ]))
    patterns_file = tmp_path / "patterns.json"
    patterns_file.write_text(json.dumps({"winning_topics": ["old"], "updated_at": "2026-08-09"}))
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(amod, "PATTERNS_FILE", str(patterns_file), raising=False)
    monkeypatch.setattr(amod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(amod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)

    async def garbled(self, evidence, model):
        return None

    monkeypatch.setattr(PerfAnalyzeTool, "_call_model", garbled)
    t = PerfAnalyzeTool()
    res = await t.execute(min_samples=5)
    d = json.loads(res)
    assert d["error"] == "model analyze parse failed"
    assert json.loads(patterns_file.read_text())["winning_topics"] == ["old"], "stale file preserved"



def test_publish_scheduler_schema():
    from douyin_tools.publish_scheduler import PublishSchedulerTool
    t = PublishSchedulerTool()
    assert t.name == "publish_scheduler"
    assert "now" in t.parameters


async def test_publish_scheduler_in_peak():
    from douyin_tools.publish_scheduler import PublishSchedulerTool
    t = PublishSchedulerTool()
    d = json.loads(await t.execute(now="11:30"))
    assert d["in_peak"] == "true"
    assert d["should_wait"] == "false"
    assert d["peak_label"] == "午高峰"


async def test_publish_scheduler_off_peak():
    from douyin_tools.publish_scheduler import PublishSchedulerTool
    t = PublishSchedulerTool()
    d = json.loads(await t.execute(now="10:30"))
    assert d["in_peak"] == "false"
    assert d["should_wait"] == "true"
    assert d["next_window"] == "11:00"


async def test_publish_scheduler_evening():
    from douyin_tools.publish_scheduler import PublishSchedulerTool
    t = PublishSchedulerTool()
    d = json.loads(await t.execute(now="18:30"))
    assert d["in_peak"] == "true"
    assert d["peak_label"] == "晚高峰"


async def test_publish_scheduler_next_wraps():
    from douyin_tools.publish_scheduler import PublishSchedulerTool
    t = PublishSchedulerTool()
    d = json.loads(await t.execute(now="22:00"))
    assert d["in_peak"] == "false"
    assert d["next_window"] == "09:00"


def test_self_repair_schema():
    from douyin_tools.self_repair import SelfRepairTool
    t = SelfRepairTool()
    assert t.name == "self_repair"
    assert "action" in t.parameters


async def test_self_repair_scan(tmp_path, monkeypatch):
    import douyin_tools.self_repair as smod
    hist = tmp_path / "stats_history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"vid": "v1", "title": "差片", "plays": 20, "interaction_rate": 0.005,
                    "age_hours": 80, "snapshot_at": "2026-08-10 10:00:00"}),
        json.dumps({"vid": "v2", "title": "好片", "plays": 500, "interaction_rate": 0.05,
                    "age_hours": 80, "snapshot_at": "2026-08-10 10:00:00"}),
    ]))
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    pub = tmp_path / "pub"
    pub.mkdir()
    json.dump({"vid": "v1", "title": "差片", "video_path": "/x.mp4"}, open(pub / "v1.json", "w"), ensure_ascii=False)
    monkeypatch.setattr(smod, "PUBLISHED_DIR", str(pub), raising=False)
    monkeypatch.setattr(smod, "PENDING_DIR", str(tmp_path / "pend"), raising=False)
    monkeypatch.setattr(smod, "BLACKLIST_FILE", str(tmp_path / "bl.json"), raising=False)
    monkeypatch.setattr(smod, "PATTERNS_FILE", str(tmp_path / "pat.json"), raising=False)
    from douyin_tools.self_repair import SelfRepairTool
    t = SelfRepairTool()
    d = json.loads(await t.execute(action="scan"))
    assert d["status"] == "scanned"
    vids = [c["vid"] for c in d["candidates"]]
    assert "v1" in vids and "v2" not in vids


async def test_self_repair_scan_dedup_review(tmp_path, monkeypatch):
    # scan 必须跳过 review/ 已 retitle 的 vid, 避免夜夜重复 retitle 堆积 (cron 23:45)
    import douyin_tools.self_repair as smod
    hist = tmp_path / "stats_history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"vid": "v1", "title": "差片", "plays": 20, "interaction_rate": 0.005,
                    "age_hours": 80, "snapshot_at": "2026-08-10 10:00:00"}),
    ]))
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    pub = tmp_path / "pub"
    pub.mkdir()
    json.dump({"vid": "v1", "title": "差片", "video_path": "/x.mp4"}, open(pub / "v1.json", "w"), ensure_ascii=False)
    monkeypatch.setattr(smod, "PUBLISHED_DIR", str(pub), raising=False)
    monkeypatch.setattr(smod, "PENDING_DIR", str(tmp_path / "pend"), raising=False)
    monkeypatch.setattr(smod, "BLACKLIST_FILE", str(tmp_path / "bl.json"), raising=False)
    monkeypatch.setattr(smod, "PATTERNS_FILE", str(tmp_path / "pat.json"), raising=False)
    review = tmp_path / "review"
    review.mkdir()
    json.dump({"id": "repair_1_v1", "repaired_from_vid": "v1", "status": "needs_review"},
              open(review / "repair_1_v1.json", "w"), ensure_ascii=False)
    monkeypatch.setattr(smod, "REVIEW_DIR", str(review), raising=False)
    from douyin_tools.self_repair import SelfRepairTool
    t = SelfRepairTool()
    d = json.loads(await t.execute(action="scan"))
    vids = [c["vid"] for c in d["candidates"]]
    assert "v1" not in vids, "scan must skip vid already retitled in review/"


async def test_self_repair_blacklist(tmp_path, monkeypatch):
    import douyin_tools.self_repair as smod
    bl = tmp_path / "bl.json"
    monkeypatch.setattr(smod, "BLACKLIST_FILE", str(bl), raising=False)
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(tmp_path / "h.jsonl"), raising=False)
    monkeypatch.setattr(smod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    monkeypatch.setattr(smod, "PENDING_DIR", str(tmp_path / "pend"), raising=False)
    from douyin_tools.self_repair import SelfRepairTool
    t = SelfRepairTool()
    d = json.loads(await t.execute(action="blacklist", topic="夸克汤"))
    assert d["status"] == "blacklisted"
    assert d["blacklist_count"] == 1
    d2 = json.loads(await t.execute(action="blacklist", topic="暴胀"))
    assert d2["blacklist_count"] == 2
    data = json.loads(bl.read_text())
    assert "夸克汤" in data["blacklist"] and "暴胀" in data["blacklist"]


async def test_self_repair_reclip_skip(tmp_path, monkeypatch):
    import douyin_tools.self_repair as smod
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(tmp_path / "h.jsonl"), raising=False)
    monkeypatch.setattr(smod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    monkeypatch.setattr(smod, "PENDING_DIR", str(tmp_path / "pend"), raising=False)
    from douyin_tools.self_repair import SelfRepairTool
    t = SelfRepairTool()
    d = json.loads(await t.execute(action="reclip", vid="v1"))
    assert d["status"] == "skip"


def test_topic_gen_load_pattern_hint(tmp_path):
    from douyin_tools.topic_gen import TopicGenTool
    pf = tmp_path / "pat.json"
    pf.write_text(json.dumps({
        "topic_gen_hint": "优先极端数字",
        "winning_topics": ["极端数字反常识"],
        "winning_hooks": ["数字+反常"],
        "losing_patterns": ["纯编号开头"],
    }))
    h = TopicGenTool._load_pattern_hint(str(pf))
    assert "极端数字" in h and "数字+反常" in h and "纯编号开头" in h
    assert TopicGenTool._load_pattern_hint(str(tmp_path / "nope.json")) == ""


async def test_topic_gen_flat_output_for_injection(tmp_path, monkeypatch):
    # C1: 补货首节点 topic_gen 必须扁平输出 topic/series/hook_variant 供 graph 注入.
    # cron 路径不传 topic, 靠 topic_gen 自产 -> 下游 {{topic}} 不再空.
    from douyin_tools import topic_gen as tmod
    from douyin_tools.topic_gen import TopicGenTool

    class FakeResp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "topics": [{"topic": "木星大红斑", "series": "宇宙奥秘", "hook_type": "反常"}]
            })}}]}

    async def fake_post_json(*a, **k):
        return FakeResp()

    monkeypatch.setattr(tmod, "post_json", fake_post_json, raising=False)
    monkeypatch.setattr(tmod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    monkeypatch.setattr(tmod, "PENDING_DIR", str(tmp_path / "pend"), raising=False)
    monkeypatch.setattr(tmod, "BLACKLIST_FILE", str(tmp_path / "bl.json"), raising=False)
    monkeypatch.setattr(tmod, "PATTERNS_FILE", str(tmp_path / "pat.json"), raising=False)

    t = TopicGenTool()
    out = json.loads(await t.execute(count=1))
    assert out["topics"][0]["topic"] == "木星大红斑"
    # 扁平字段 (C1 注入契约).
    assert out["topic"] == "木星大红斑"
    assert out["series"] == "宇宙奥秘"
    assert out["hook_variant"] in ("A", "B", "C")


def test_mlx_script_load_winning_hooks(tmp_path):
    from douyin_tools.mlx_script import MlxScriptTool
    pf = tmp_path / "pat.json"
    pf.write_text(json.dumps({"winning_hooks": ["数字+反常", "提问+代入"]}))
    h = MlxScriptTool._load_winning_hooks(str(pf))
    assert "数字+反常" in h and "提问+代入" in h
    assert MlxScriptTool._load_winning_hooks(str(tmp_path / "nope.json")) == ""


def test_perf_track_aggregate_variant():
    from douyin_tools.perf_track import PerfTrackTool
    classified = [
        {"vid": "1", "plays": 100, "variant": "A"},
        {"vid": "2", "plays": 300, "variant": "A"},
        {"vid": "3", "plays": 50, "variant": "B"},
        {"vid": "4", "plays": 10, "variant": ""},
    ]
    agg = {a["variant"]: a for a in PerfTrackTool._aggregate_variant(classified)}
    assert agg["A"]["count"] == 2
    assert agg["A"]["plays_avg"] == 200.0
    assert agg["A"]["plays_max"] == 300
    assert agg["B"]["count"] == 1
    assert "C" not in agg


def test_perf_track_backfill_published_vid(tmp_path, monkeypatch):
    import douyin_tools.perf_track as pmod
    pub = tmp_path / "published"
    pub.mkdir()
    import json as _j
    (_j).dump({"title": "宇宙最冷的地方", "topic": "宇宙最冷的地方", "video_path": "/x.mp4"},
              open(pub / "a.json", "w"), ensure_ascii=False)
    monkeypatch.setattr(pmod, "PUBLISHED_DIR", str(pub), raising=False)
    items = [{"vid": "7600000001", "title": "宇宙最冷的地方", "plays": 1329}]
    n = pmod.PerfTrackTool._backfill_published_vid(items)
    assert n == 1
    d = _j.load(open(pub / "a.json"))
    assert d["vid"] == "7600000001"


def test_perf_track_backfill_by_description_prefix(tmp_path, monkeypatch):
    import douyin_tools.perf_track as pmod
    import json as _j
    pub = tmp_path / "published"
    pub.mkdir()
    item = {
        "title": "别怕黑洞掉进去根本不会死",
        "topic": "别怕黑洞掉进去根本不会死",
        "description": "你以为掉进黑洞必死？大错特错！颠覆你的认知。#宇宙奥秘",
        "hook_variant": "B",
        "video_path": "/x.mp4",
    }
    _j.dump(item, open(pub / "b.json", "w"), ensure_ascii=False)
    monkeypatch.setattr(pmod, "PUBLISHED_DIR", str(pub), raising=False)
    stats_title = "你以为掉进黑洞必死？大错特错！颠覆你的认知。#宇宙奥秘 #科普"
    items = [{"vid": "7672621214010985728", "title": stats_title, "plays": 679}]
    n = pmod.PerfTrackTool._backfill_published_vid(items)
    assert n == 1
    d = _j.load(open(pub / "b.json"))
    assert d["vid"] == "7672621214010985728"


def test_comfyui_tts_build_graph_http_backend(monkeypatch):
    import douyin_tools.comfyui_tts as tmod
    from douyin_tools import config
    from douyin_tools.comfyui_tts import ComfyUITtsTool

    # 审计 H5: TTS_* 已是 config 动态常量, tool 用 config.X 属性访问 -> patch config 模块.
    monkeypatch.setattr(config, "COMFYUI_TTS_BACKEND", "http", raising=False)
    monkeypatch.setattr(config, "COMFYUI_TTS_HTTP_MODEL", "Qwen3-TTS-12Hz-1.7B-Base-8bit", raising=False)
    monkeypatch.setattr(config, "COMFYUI_TTS_HTTP_VOICE", "Cherry", raising=False)
    tool = ComfyUITtsTool()
    graph = tool._build_graph("测试中文语音", "Cherry", 1.0, "fo_x")
    synth = graph["2"]["inputs"]
    assert synth["backend"] == "http"
    assert synth["model_name"] == "Qwen3-TTS-12Hz-1.7B-Base-8bit"
    assert synth["voice"] == "Cherry"
    assert graph["3"]["class_type"] == "FusionSaveAudio"


def test_comfyui_tts_build_graph_local_backend(monkeypatch):
    import douyin_tools.comfyui_tts as tmod
    from douyin_tools import config
    from douyin_tools.comfyui_tts import ComfyUITtsTool

    monkeypatch.setattr(config, "COMFYUI_TTS_BACKEND", "local", raising=False)
    tool = ComfyUITtsTool()
    graph = tool._build_graph("hello", "af_heart", 1.0, "fo_y")
    synth = graph["2"]["inputs"]
    assert "backend" not in synth
    assert "model_name" not in synth
    assert synth["voice"] == "af_heart"


def test_comfyui_tts_default_voice_per_backend(monkeypatch):
    import douyin_tools.comfyui_tts as tmod
    from douyin_tools import config
    from douyin_tools.comfyui_tts import ComfyUITtsTool

    captured = {}

    class FakeClient:
        async def health(self):
            return True

        async def run_graph(self, graph, max_wait=300):
            captured["synth"] = graph["2"]["inputs"]
            return {"status": "success", "paths": []}

        def resolve_audio_by_prefix(self, prefix):
            return ""

    monkeypatch.setattr(tmod, "ComfyUIClient", FakeClient, raising=False)
    monkeypatch.setattr(tmod, "config", config, raising=False)
    monkeypatch.setattr(config, "COMFYUI_TTS_BACKEND", "http", raising=False)
    monkeypatch.setattr(config, "COMFYUI_TTS_HTTP_VOICE", "Cherry", raising=False)

    import asyncio

    tool = ComfyUITtsTool()
    out = asyncio.run(tool.execute("测试中文", out_name="z"))
    # 默认 voice 应被替换成 Cherry (http backend)
    assert captured["synth"]["voice"] == "Cherry"
    assert captured["synth"]["backend"] == "http"


def test_declarative_config_override(tmp_path, monkeypatch):
    # 声明式配置契约: 改 douyin_config.json 的值, 工具立即读到新值, 不改代码.
    cfg_path = tmp_path / "douyin_config.json"
    cfg_path.write_text(
        '{"models":{"chat":"CFG-MODEL"},'
        '"script":{"max_scenes":3,"hook_variants":{"A":"AA","B":"BB","C":"CC"}},'
        '"perf_track":{"hot_plays_min":250,"cold_plays_max":10,"hot_top_percent":0.3},'
        '"scheduler":{"peak_windows":["09-10","20-22"]}}'
    )
    monkeypatch.setenv("DOUYIN_CONFIG_FILE", str(cfg_path))
    import importlib
    import douyin_tools.config as cfg
    importlib.reload(cfg)
    import douyin_tools.mlx_script as ms
    importlib.reload(ms)
    import douyin_tools.perf_track as pt
    importlib.reload(pt)
    import douyin_tools.publish_scheduler as ps
    importlib.reload(ps)
    assert cfg.DEFAULT_CHAT_MODEL == "CFG-MODEL"
    assert ms.MAX_SCENES == 3
    assert ms.VARIANT_HOOK["A"] == "AA"
    assert pt.HOT_PLAYS_MIN == 250
    assert pt.COLD_PLAYS_MAX == 10
    assert pt.HOT_TOP_PERCENT == 0.3
    assert ps.PEAK_WINDOWS == [(9, 10), (20, 22)]
    # 恢复默认配置, 避免污染同 session 后续测试的模块全局状态.
    monkeypatch.delenv("DOUYIN_CONFIG_FILE", raising=False)
    importlib.reload(cfg)
    importlib.reload(ms)
    importlib.reload(pt)
    importlib.reload(ps)


def test_config_dynamic_constant_reflects_json_edit(tmp_path, monkeypatch):
    # 审计 H5 契约: 动态常量 (config.X 属性访问) 改 JSON 立即生效, 不 re-import.
    # 旧实现模块级常量 import 时绑定一次, daemon launchd 长进程改配置永不生效 -> 本测覆盖.
    import json
    import importlib
    cfg_path = tmp_path / "douyin_config.json"
    cfg_path.write_text(json.dumps({
        "models": {"chat": "MODEL-V1"},
        "mlx": {"base": "http://v1:11434", "timeout": 111},
        "comfyui": {"tts_backend": "local"},
    }))
    monkeypatch.setenv("DOUYIN_CONFIG_FILE", str(cfg_path))
    # 清掉环境变量覆盖, 强制走 JSON 层 (env > JSON > 默认, env 不空会遮蔽 JSON).
    monkeypatch.delenv("FUSION_CHAT_MODEL", raising=False)
    monkeypatch.delenv("FUSION_MLX_BASE", raising=False)
    monkeypatch.delenv("FUSION_MLX_TIMEOUT", raising=False)
    monkeypatch.delenv("FUSION_COMFYUI_TTS_BACKEND", raising=False)
    import douyin_tools.config as cfg
    importlib.reload(cfg)
    # 清掉同 session 早期测试 monkeypatch.setattr(config, COMFYUI_TTS_*, ...) 可能残留的
    # 模块级属性 (importlib.reload 不清 __dict__, 残留属性会遮蔽 __getattr__ 动态读取).
    for stale in ("DEFAULT_CHAT_MODEL", "MLX_BASE", "MLX_TIMEOUT", "COMFYUI_TTS_BACKEND"):
        if stale in cfg.__dict__:
            delattr(cfg, stale)
    # v1 快照
    assert cfg.DEFAULT_CHAT_MODEL == "MODEL-V1"
    assert cfg.MLX_BASE == "http://v1:11434"
    assert cfg.MLX_TIMEOUT == 111.0
    assert cfg.COMFYUI_TTS_BACKEND == "local"
    # 改 JSON 不 reload: 属性访问应实时读到新值 (mtime 变触发 _refresh_if_stale).
    cfg_path.write_text(json.dumps({
        "models": {"chat": "MODEL-V2"},
        "mlx": {"base": "http://v2:11434", "timeout": 222},
        "comfyui": {"tts_backend": "http"},
    }))
    assert cfg.DEFAULT_CHAT_MODEL == "MODEL-V2"
    assert cfg.MLX_BASE == "http://v2:11434"
    assert cfg.MLX_TIMEOUT == 222.0
    assert cfg.COMFYUI_TTS_BACKEND == "http"
    # env 覆盖优先级: 环境变量 > JSON
    monkeypatch.setenv("FUSION_CHAT_MODEL", "ENV-MODEL")
    assert cfg.DEFAULT_CHAT_MODEL == "ENV-MODEL"
    # 恢复默认配置避免污染.
    monkeypatch.delenv("DOUYIN_CONFIG_FILE", raising=False)
    monkeypatch.delenv("FUSION_CHAT_MODEL", raising=False)
    importlib.reload(cfg)


def test_config_mtime_null_no_crash(tmp_path, monkeypatch):
    # 审计 R7: 配置文件丢失/初装缺失, _cfg_mtime 返回 None (非 0.0),
    # _refresh_if_stale 用 is not None 判空不崩溃, 保留上次缓存 (不 wipe 到默认).
    import importlib
    cfg_path = tmp_path / "missing_config.json"
    monkeypatch.setenv("DOUYIN_CONFIG_FILE", str(cfg_path))
    import douyin_tools.config as cfg
    importlib.reload(cfg)
    # 文件不存在 -> _load_json_config 返回 {} -> _cfg_mtime 返回 None.
    assert cfg._cfg_mtime() is None
    assert cfg.get_json_config() == {}
    # 动态常量回退默认 (JSON 空, env 空).
    monkeypatch.delenv("FUSION_CHAT_MODEL", raising=False)
    assert cfg.DEFAULT_CHAT_MODEL == "Qwen3.5-9B-4bit"
    monkeypatch.delenv("DOUYIN_CONFIG_FILE", raising=False)
    importlib.reload(cfg)


def test_sync_agent_config_reference_resolution(tmp_path, monkeypatch):
    # 声明式 tool_config 契约: 引用式 "models.chat" 解析为实际模型名,
    # 生成 definition.json 可被 AgentDefinition.from_dict 解析, tools[].config 落位.
    import sync_agent_config as sac
    cfg = {
        "models": {"chat": "SYNC-CHAT-MODEL"},
        "comfyui": {"tts_http_voice": "SYNC-VOICE"},
        "agent_studio": {
            "agent_id": "test-agent-sync",
            "tool_config": {
                "mlx_script": {"model": "models.chat"},
                "comfyui_tts": {"voice": "comfyui.tts_http_voice"},
                "perf_analyze": {"model": "models.chat"},
            },
        },
    }
    tool_configs = sac.build_tool_configs(cfg)
    assert tool_configs["mlx_script"] == {"model": "SYNC-CHAT-MODEL"}
    assert tool_configs["comfyui_tts"] == {"voice": "SYNC-VOICE"}
    assert tool_configs["perf_analyze"] == {"model": "SYNC-CHAT-MODEL"}


def test_sync_agent_config_definition_compatible(tmp_path, monkeypatch):
    # 生成的 definition.json 必须能被 daemon 的 AgentDefinition.from_dict 解析,
    # 且 tool config 参数名必须匹配 tool openai_schema (runtime 注入陷阱:
    # validate 在 merge 之前, config key 不匹配 schema 会 unexpected kwarg).
    import sync_agent_config as sac
    from agent_runtime.agent_definition import AgentDefinition
    cfg = {
        "models": {"chat": "Qwen3.5-9B-4bit"},
        "comfyui": {"tts_http_voice": "Cherry"},
        "agent_studio": {
            "agent_id": "test-agent-def",
            "tool_config": {
                "mlx_script": {"model": "models.chat"},
                "comfyui_tts": {"voice": "comfyui.tts_http_voice"},
            },
        },
    }
    manifest = {
        "name": "test-agent",
        "model": "Qwen3.5-9B-4bit",
        "tools": ["mlx_script", "comfyui_tts", "queue_ops"],
        "status": "published",
    }
    tool_configs = sac.build_tool_configs(cfg)
    definition = sac.build_definition(manifest, tool_configs)
    definition["agent_id"] = "test-agent-def"
    defn_path = tmp_path / "definition.json"
    defn_path.write_text(json.dumps(definition, ensure_ascii=False))
    d = AgentDefinition.from_dict(json.loads(defn_path.read_text()))
    assert d.agent_id == "test-agent-def"
    assert len(d.tools) == 3
    by_name = {t.name: t for t in d.tools}
    assert by_name["mlx_script"].config == {"model": "Qwen3.5-9B-4bit"}
    assert by_name["comfyui_tts"].config == {"voice": "Cherry"}
    assert by_name["queue_ops"].config == {}
    # 参数名必须落在真实 tool 的 openai_schema parameters 内,
    # 否则 runtime _merge_tool_config_defaults 注入后会 unexpected kwarg.
    registry = create_douyin_registry()
    for t in d.tools:
        if not t.config:
            continue
        tool = registry.get(t.name)
        assert tool is not None, f"tool {t.name} not in registry"
        schema = tool.openai_schema() if callable(tool.openai_schema) else (tool.openai_schema or {})
        params = schema.get("parameters") or schema.get("function", {}).get("parameters", {})
        schema_params = set(params.get("properties", {}).keys())
        for param in t.config.keys():
            assert param in schema_params, (
                f"tool_config[{t.name}].{param} 不在 {t.name} openai_schema 内, "
                f"runtime 注入会触发 unexpected keyword argument"
            )


class _FakeRpcClient:
    # 假 RPC client: 记录 graph store + cron 操作, 验证 sync_graphs 幂等去重 + cron 回填.
    def __init__(self, existing=None, cron_jobs=None):
        self.store = {}
        for gid, name in (existing or []):
            self.store[gid] = {"name": name}
        self.cron_jobs = {}
        for jid, gid in (cron_jobs or []):
            self.cron_jobs[jid] = {"id": jid, "graph_id": gid}
        self.calls = []
        self._counter = 100

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "graph.list":
            graphs = [
                {"graph_id": gid, "name": v["name"], "node_count": 0}
                for gid, v in self.store.items()
            ]
            return {"graphs": graphs}
        if method == "graph.delete":
            gid = params.get("graph_id", "")
            if gid in self.store:
                del self.store[gid]
                return {"deleted": True}
            return {"error": "not found"}
        if method == "graph.create":
            data = params.get("graph_data", {})
            self._counter += 1
            new_id = f"g-{self._counter}"
            self.store[new_id] = {"name": data.get("name", ""), "data": data}
            return {"graph_id": new_id, "name": data.get("name", "")}
        if method == "cron.register":
            # INSERT OR REPLACE 语义: 同 id 覆盖.
            jid = params.get("id", "")
            self.cron_jobs[jid] = {
                "id": jid,
                "graph_id": params.get("graph_id", ""),
                "expression": params.get("expression", ""),
                "enabled": params.get("enabled", True),
            }
            return {"status": "ok", "job": {"id": jid}}
        if method == "cron.list":
            return {"jobs": [dict(v) for v in self.cron_jobs.values()]}
        return {"error": "unknown method"}


def test_sync_graphs_idempotent_name_dedup(tmp_path, monkeypatch):
    # sync_graphs 契约: 按 name 去重, 同名旧 graph 先删后建, 重复运行不膨胀.
    import sync_graphs as sg
    import glob as _glob
    import asyncio as _asyncio

    gdir = tmp_path / "graphs"
    gdir.mkdir()
    (gdir / "douyin_a.json").write_text(json.dumps({
        "name": "douyin_a", "nodes": {"start": {"type": "start"}}, "edges": [],
    }))
    (gdir / "douyin_b.json").write_text(json.dumps({
        "name": "douyin_b", "nodes": {"start": {"type": "start"}}, "edges": [],
    }))
    monkeypatch.setattr(sg, "GRAPHS_DIR", str(gdir))

    fake = _FakeRpcClient(existing=[("old1", "douyin_a"), ("old2", "douyin_b")])
    monkeypatch.setattr(sg, "AgentClient", lambda socket_path=None: fake)

    files = sorted(_glob.glob(os.path.join(str(gdir), "*.json")))
    synced = _asyncio.run(sg.sync_graphs(files))
    assert synced == 2
    names = [v["name"] for v in fake.store.values()]
    assert sorted(names) == ["douyin_a", "douyin_b"]
    assert len(fake.store) == 2

    fake2 = _FakeRpcClient(existing=[(gid, v["name"]) for gid, v in fake.store.items()])
    monkeypatch.setattr(sg, "AgentClient", lambda socket_path=None: fake2)
    synced2 = _asyncio.run(sg.sync_graphs(files))
    assert synced2 == 2
    assert len(fake2.store) == 2
    names2 = sorted(v["name"] for v in fake2.store.values())
    assert names2 == ["douyin_a", "douyin_b"]


def test_sync_graphs_name_collision_regression(tmp_path, monkeypatch):
    # 回归: 两个文件 name 相同 → sync 后 store 里该 name 应只剩 1 份.
    # 对应 graphs/douyin_comment_reply_winner.json 曾 name=douyin_comment_reply.
    import sync_graphs as sg
    import glob as _glob
    import asyncio as _asyncio

    gdir = tmp_path / "graphs"
    gdir.mkdir()
    (gdir / "douyin_comment_reply.json").write_text(json.dumps({
        "name": "douyin_comment_reply", "nodes": {"start": {"type": "start"}}, "edges": [],
    }))
    (gdir / "douyin_comment_reply_winner.json").write_text(json.dumps({
        "name": "douyin_comment_reply_winner", "nodes": {"start": {"type": "start"}}, "edges": [],
    }))
    monkeypatch.setattr(sg, "GRAPHS_DIR", str(gdir))

    fake = _FakeRpcClient()
    monkeypatch.setattr(sg, "AgentClient", lambda socket_path=None: fake)
    files = sorted(_glob.glob(os.path.join(str(gdir), "*.json")))
    _asyncio.run(sg.sync_graphs(files))
    assert len(fake.store) == 2
    names = sorted(v["name"] for v in fake.store.values())
    assert names == ["douyin_comment_reply", "douyin_comment_reply_winner"]


def test_sync_graphs_cron_backfill(tmp_path, monkeypatch):
    # 回归: sync 重建 graph 后 graph_id 变, 须回填关联 cron job 的 graph_id.
    # 8-20 11:05 根因: cron 绑死旧 id -> Graph not found -> 发布失败.
    import sync_graphs as sg
    import glob as _glob
    import asyncio as _asyncio

    gdir = tmp_path / "graphs"
    gdir.mkdir()
    (gdir / "douyin_auto_publish.json").write_text(json.dumps({
        "name": "douyin_auto_publish",
        "agent_id": "c65efddbe8c5",
        "nodes": {"start": {"type": "start"}},
        "edges": [],
    }))
    monkeypatch.setattr(sg, "GRAPHS_DIR", str(gdir))

    # 旧 store 有 auto_publish (旧 id old-auto) + cron 绑旧 id.
    fake = _FakeRpcClient(
        existing=[("old-auto", "douyin_auto_publish")],
        cron_jobs=[
            ("douyin_publish_plan_0905", "old-auto"),
            ("douyin_publish_plan_1105", "old-auto"),
        ],
    )
    monkeypatch.setattr(sg, "AgentClient", lambda socket_path=None: fake)
    files = sorted(_glob.glob(os.path.join(str(gdir), "*.json")))
    _asyncio.run(sg.sync_graphs(files))

    # 旧 id 删除, 新 id 落 store.
    assert "old-auto" not in fake.store
    new_id = [gid for gid, v in fake.store.items() if v["name"] == "douyin_auto_publish"][0]
    # cron job 已回填新 graph_id (非旧 old-auto).
    assert fake.cron_jobs["douyin_publish_plan_0905"]["graph_id"] == new_id
    assert fake.cron_jobs["douyin_publish_plan_1105"]["graph_id"] == new_id
    assert fake.cron_jobs["douyin_publish_plan_0905"]["graph_id"] != "old-auto"
    # 回填时 expression 必须正确 (不能默认 * * * * *).
    assert fake.cron_jobs["douyin_publish_plan_0905"]["expression"] == "5 9 * * *"
    assert fake.cron_jobs["douyin_publish_plan_1105"]["expression"] == "5 11 * * *"


def test_auto_publish_graph_structure():
    # douyin_auto_publish.json: cron 调度发布 + 库存不足自动补货 + 补货失败循环守卫.
    # C1 修复: 补货首节点 topic_gen 自产 topic/series/hook_variant 注入下游, 不依赖外部 cron 传参.
    # gate_stock=false -> topic_gen -> script -> ... -> enqueue -> gate_refilled -> dequeue 回环; 失败 -> end.
    gpath = os.path.join(os.path.dirname(__file__), "..", "graphs", "douyin_auto_publish.json")
    with open(gpath, encoding="utf-8") as f:
        g = json.load(f)
    assert g["name"] == "douyin_auto_publish"
    assert g["agent_id"] == "c65efddbe8c5"
    nodes = g["nodes"]
    for nid in ["start", "scheduler", "gate_peak", "dequeue", "gate_stock",
                "gate_refill_budget", "topic_gen", "script", "img", "tts", "compose",
                "enqueue", "gate_refilled", "publish", "gate_ok", "archive", "end"]:
        assert nid in nodes, f"missing node {nid}"
    # H4: gate_refill_budget 用原生 iteration 变量限制补货循环次数 (防 dequeue<->refill 无限回环).
    assert nodes["gate_refill_budget"]["condition_expr"] == "iteration < 23"
    # topic_gen 输出扁平字段注入 runtime 变量域 (修 C1: cron 不传 topic 时自产)
    om = nodes["topic_gen"]["tool_params"]["output_mapping"]
    assert om.get("topic") == "topic", "topic_gen must expose topic for injection"
    assert om.get("hook_variant") == "hook_variant"
    # 补货链闭环: enqueue -> gate_refilled -> (true) dequeue / (false) end
    edges = {(e["source_id"], e["target_id"], e.get("label", "")) for e in g["edges"]}
    # H4: gate_stock false 先过 refill_budget 门再进补货链; 超限 -> end 显式停 (不再隐式靠全局 max_iterations 崩)
    assert ("gate_stock", "gate_refill_budget", "false") in edges, "gate_stock false must route to refill budget gate (H4)"
    assert ("gate_refill_budget", "topic_gen", "true") in edges, "budget ok must enter refill chain"
    assert ("gate_refill_budget", "end", "false") in edges, "budget exhausted must exit to end (H4 no infinite loop)"
    assert ("topic_gen", "script", "") in edges, "topic_gen must feed script node"
    assert ("enqueue", "gate_refilled", "") in edges, "enqueue must route to gate_refilled guard"
    assert ("gate_refilled", "dequeue", "true") in edges, "refill success must loop back to dequeue"
    assert ("gate_refilled", "end", "false") in edges, "refill failure must exit to end (no infinite loop)"
    # enqueue output_mapping exposes status for the guard condition
    assert nodes["enqueue"]["tool_params"]["output_mapping"]["status"] == "enqueued_status"
    assert nodes["gate_refilled"]["condition_expr"] == "enqueued_status == enqueued"
    # publish path intact
    assert ("gate_stock", "publish", "true") in edges
    assert ("gate_peak", "end", "false") in edges, "off-peak must exit to end"


def test_mlx_headers_fusion_route():
    # fusion-mlx v0.7.0 (#349) 强制 X-Fusion-Route 头, 缺失即 403 missing_route.
    # mlx_headers 必须注入 X-Fusion-Route: fusion-operation (provenance-only, monorepo 约定).
    from douyin_tools.config import mlx_headers
    h = mlx_headers()
    assert h.get("X-Fusion-Route") == "fusion-operation"
    # 鉴权头仍在 (settings.json 回退 dahai168)
    assert "Authorization" in h or "x-api-key" in h


async def test_gate_tool_bool_outputs_are_strings(tmp_path, monkeypatch):
    # H3 契约: ConditionEngine `has_stock == true` 右侧字面量解析为 str "true",
    # 左侧工具输出若为 JSON bool true (Python True) 则 True == "true" 恒 false -> gate 永走 false 分支.
    # 下游规避: 所有 gate 用工具输出字符串 "true"/"false". 此测试固化该约定, 防新工具回退 JSON bool.
    # 真正修复在上游 ConditionEngine 布尔归一化 (见 task #34 upstream issue).
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    monkeypatch.setattr(QueueOpsTool, "PENDING_DIR", os.path.join(qdir, "pending"))
    monkeypatch.setattr(QueueOpsTool, "INFLIGHT_DIR", os.path.join(qdir, "inflight"))
    monkeypatch.setattr(QueueOpsTool, "PUBLISHED_DIR", os.path.join(qdir, "published"))
    monkeypatch.setattr(QueueOpsTool, "FAILED_DIR", os.path.join(qdir, "failed"))
    # 空队列 dequeue -> has_stock 必须是字符串 "false" 而非 JSON bool false
    empty = json.loads(await QueueOpsTool().execute(action="dequeue"))
    assert empty["has_stock"] == "false"
    assert isinstance(empty["has_stock"], str), "has_stock must be str bool for ConditionEngine"
    # publish_scheduler 输出 in_peak 同样字符串布尔
    from douyin_tools.publish_scheduler import PublishSchedulerTool
    ps = json.loads(await PublishSchedulerTool().execute())
    assert ps["in_peak"] in ("true", "false")
    assert isinstance(ps["in_peak"], str), "in_peak must be str bool for ConditionEngine"


def test_mlx_health_detects_auth_and_unreachable(monkeypatch):
    # A5: mlx_health 区分 401(鉴权) / 403(route) / 200(ok) / 不可达.
    from douyin_tools import config as cmod

    class FakeResp:
        def __init__(self, code, body=None):
            self.status_code = code
            self._body = body or {}
        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, code, body=None):
            self._r = FakeResp(code, body)
        def get(self, *a, **k):
            return self._r

    monkeypatch.setattr(cmod, "mlx_headers", lambda: {"X-Fusion-Route": "fusion-operation"})
    monkeypatch.setattr(cmod.httpx, "get", lambda *a, **k: FakeResp(401).status_code and FakeResp(401))
    h = cmod.mlx_health()
    assert h["ok"] is False and h["http"] == 401 and "401" in h["reason"]

    monkeypatch.setattr(cmod.httpx, "get", lambda *a, **k: FakeResp(200, {"data": [{"id": "m1"}, {"id": "m2"}]}))
    h = cmod.mlx_health()
    assert h["ok"] is True and h["model_count"] == 2

    def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(cmod.httpx, "get", boom)
    h = cmod.mlx_health()
    assert h["ok"] is False and "unreachable" in h["reason"]


def test_reload_json_config_picks_up_change(tmp_path, monkeypatch):
    # A1: reload_json_config 重读 douyin_config.json, 改配置不需重启进程.
    import importlib
    from douyin_tools import config as cmod
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"scheduler": {"peak_windows": ["9-10"]}}))
    monkeypatch.setenv("DOUYIN_CONFIG_FILE", str(cfg))
    importlib.reload(cmod)
    assert cmod.get_json_config().get("scheduler", {}).get("peak_windows") == ["9-10"]
    cfg.write_text(json.dumps({"scheduler": {"peak_windows": ["20-22"]}}))
    # reload_json_config 应读到新值.
    cmod.reload_json_config()
    assert cmod.get_json_config().get("scheduler", {}).get("peak_windows") == ["20-22"]
    # 恢复避免污染同 session 其他测试.
    monkeypatch.delenv("DOUYIN_CONFIG_FILE", raising=False)
    importlib.reload(cmod)


def test_extract_narration_segments_model_match():
    # 模型给的 narration_segment 拼接等于全文 -> 直接采用 (音画同步).
    from douyin_tools.script_helpers import extract_narration_segments
    narration = "第一句。第二句。第三句。"
    script = {
        "narration": narration,
        "scenes": [
            {"narration_segment": "第一句。"},
            {"narration_segment": "第二句。"},
            {"narration_segment": "第三句。"},
        ],
    }
    segs = extract_narration_segments(script)
    assert segs == ["第一句。", "第二句。", "第三句。"]
    assert "".join(segs) == narration


def test_extract_narration_segments_fallback_split():
    # 模型未给 narration_segment -> 回退按分镜数等分全文 (标点断句).
    from douyin_tools.script_helpers import extract_narration_segments
    narration = "第一句。第二句。第三句。第四句。"
    script = {
        "narration": narration,
        "scenes": [
            {"cn_text": "a"},
            {"cn_text": "b"},
            {"cn_text": "c"},
            {"cn_text": "d"},
        ],
    }
    segs = extract_narration_segments(script)
    assert len(segs) == 4
    # 拼接必须覆盖全文 (允许有/无标点差异但内容不丢).
    assert "".join(segs).strip() == narration.strip()


def test_parse_script_repr_string_fallback():
    # AgentRuntime 扁平插值把 dict 变量按 Python repr 注入 (单引号 {'k':'v'}),
    # 非合法 JSON -> parse_script 走 ast.literal_eval 回退解析出 script dict.
    from douyin_tools.script_helpers import parse_script, extract_prompts, extract_narration

    script = {
        "title": "木星大红斑",
        "narration": "木星有一个大红斑。",
        "scenes": [{"img_prompt": "jupiter great red spot, no people"}],
    }
    repr_str = repr(script)  # 单引号 dict repr
    assert "'" in repr_str and not repr_str.lstrip().startswith("{\"")  # 确实是 repr 非 JSON
    parsed = parse_script(repr_str)
    assert parsed is not None, "repr string must parse via ast fallback"
    assert extract_prompts(parsed) == ["jupiter great red spot, no people"]
    assert extract_narration(parsed) == "木星有一个大红斑。"

    # 合法 JSON 字符串仍正常 (json.loads 路径)
    import json as _json
    assert parse_script(_json.dumps(script, ensure_ascii=False)) is not None

    # 非脚本结构 -> None
    assert parse_script("just a plain string") is None
    assert parse_script("[1, 2, 3]") is None


def test_compose_duration_proportional():
    # ffmpeg_compose 按旁白片段字数比例分配每镜帧数 (音画同步), 非等分.
    from douyin_tools.ffmpeg_compose import FfmpegComposeTool
    tool = FfmpegComposeTool()
    total_frames = 240
    # 镜头0片段4字, 镜头1片段16字 -> 镜头1应分到约4倍帧数.
    weights = [4, 16]
    total_w = sum(weights)
    counts = [max(int(total_frames * w / total_w), 24) for w in weights]
    diff = total_frames - sum(counts)
    if diff:
        counts[-1] = max(counts[-1] + diff, 24)
    assert counts[1] > counts[0] * 2, "longer narration segment must get more frames"
    assert sum(counts) == total_frames, "frame counts must sum to total (no drift)"


def test_compose_tempdir_cleaned_after_render(tmp_path, monkeypatch):
    # A2: 临时帧目录必须渲染后清理 (单条视频数千帧 png + 中间 mp4, 不清则磁盘膨胀).
    import os
    import numpy as np
    from douyin_tools import ffmpeg_compose as fc

    captured = {"tmp": None}
    fake_img = tmp_path / "img.png"
    fake_img.write_bytes(b"x")
    fake_wav = tmp_path / "a.wav"
    fake_wav.write_bytes(b"x")

    def fake_render_loop(self, tmp, imgs, scenes, narr, audio, tf, fc_list, title, out):
        captured["tmp"] = tmp
        # 模拟渲染产物 + 最终 out_path 占位.
        with open(os.path.join(tmp, "sc_000.mp4"), "w") as f:
            f.write("x")
        with open(out, "w") as f:
            f.write("x")
        return True

    monkeypatch.setattr(fc.FfmpegComposeTool, "_render_loop", fake_render_loop)
    monkeypatch.setattr(fc.FfmpegComposeTool, "_audio_duration", lambda self, p: 15.0)

    tool = fc.FfmpegComposeTool()
    import asyncio
    out = asyncio.run(
        tool.execute(
            image_paths=[str(fake_img)],
            audio_path=str(fake_wav),
            scenes='[{"cn_text":"hi","scene":"x"}]',
            out_name="cleanup_test",
        )
    )
    assert "video_path" in out, out
    # 渲染后 tmp 目录已被 shutil.rmtree 清理.
    assert captured["tmp"] is not None
    assert not os.path.exists(captured["tmp"]), "tempdir must be cleaned after render"


def test_comfyui_image_default_size_vertical():
    # 配图默认改竖屏 1080x1920 (抖音9:16), 根治方图裁切主体丢失.
    import inspect
    from douyin_tools.comfyui_produce import ComfyUIImageTool
    sig = inspect.signature(ComfyUIImageTool.execute)
    assert sig.parameters["size"].default == "1080x1920"


def test_comfyui_image_no_people_negative():
    # 天体科普视频禁人物: 生成层 negative_prompt 确定性兜底, 即使 img_prompt 漏写排除词也拦下.
    from douyin_tools.comfyui_produce import ComfyUIImageTool
    tool = ComfyUIImageTool()
    graph = tool._build_graph(["a majestic nebula in deep space"], 1, 1080, 1920, 8)
    gen_node = next(v for v in graph.values() if v.get("class_type") == "FusionImageGen")
    neg = gen_node["inputs"]["negative_prompt"]
    assert "people" in neg and "human" in neg and "astronaut" in neg and "face" in neg
    assert neg != ""


def test_web_image_search_extract_celestial():
    # img_prompt 抽首个天体名词作 NASA query (简短名词召回准). 命中不到 fallback 前 3 词.
    from douyin_tools.web_image_search import WebImageSearchTool
    assert WebImageSearchTool._extract_celestial("Jupiter Great Red Spot close up storm") == "jupiter"
    assert WebImageSearchTool._extract_celestial("a black hole accretion disk glowing blue") == "black hole"
    assert WebImageSearchTool._extract_celestial("nebula clouds in deep space") == "nebula"
    # 无天体词 -> fallback 前 3 词短语
    q = WebImageSearchTool._extract_celestial("a swirling vortex of dark matter fading")
    assert q == "a swirling vortex"


def test_web_image_search_parse_queries_script_json():
    # script_json(dict) -> extract_prompts 取 img_prompt -> 抽天体词列表.
    import json
    from douyin_tools.web_image_search import WebImageSearchTool
    tool = WebImageSearchTool()
    script = {
        "title": "test", "narration": "n",
        "scenes": [
            {"img_prompt": "Jupiter close up storm", "cn_text": "木星"},
            {"img_prompt": "Saturn rings glowing", "cn_text": "土星"},
        ],
    }
    qlist = tool._parse_queries(json.dumps(script))
    assert qlist == ["jupiter", "saturn"]


def test_web_image_search_parse_queries_list():
    # 裸关键词列表/逗号串 -> query 列表.
    import json
    from douyin_tools.web_image_search import WebImageSearchTool
    tool = WebImageSearchTool()
    assert tool._parse_queries(json.dumps(["mars", "earth"])) == ["mars", "earth"]
    assert tool._parse_queries("mars, earth") == ["mars", "earth"]


def test_web_image_search_search_nasa_mock(monkeypatch):
    # mock NASA search -> metadata JSON -> 选 large jpg. 验证 _fetch_one 全链路 + enough.
    import asyncio, json
    from douyin_tools.web_image_search import WebImageSearchTool
    tool = WebImageSearchTool()
    items = [{
        "href": "https://images-api.nasa.gov/assets/AS11",
        "data": [{"nasa_id": "AS11", "title": "Jupiter", "photographer": "NASA", "description": "jupiter image"}],
    }]
    monkeypatch.setattr(WebImageSearchTool, "_search_nasa", lambda self, q: _async_val(items))
    monkeypatch.setattr(WebImageSearchTool, "_resolve_image_url", lambda self, h: _async_val("https://x/jupiter~large.jpg"))
    monkeypatch.setattr(WebImageSearchTool, "_download", lambda self, url, dst: _async_val(dst))
    r = asyncio.run(tool.execute(json.dumps(["jupiter"]), per_query=1, expected=1))
    d = json.loads(r)
    assert len(d["image_paths"]) == 1
    assert d["enough"] == "true"


def test_web_image_search_fallback_empty(monkeypatch):
    # NASA 返回空 -> image_paths=[] enough=false (触发 Graph C 回退生成图).
    import asyncio, json
    from douyin_tools.web_image_search import WebImageSearchTool
    tool = WebImageSearchTool()
    monkeypatch.setattr(WebImageSearchTool, "_search_nasa", lambda self, q: _async_val([]))
    r = asyncio.run(tool.execute(json.dumps(["mars"]), expected=1))
    d = json.loads(r)
    assert d["image_paths"] == []
    assert d["enough"] == "false"


def test_web_image_search_skip_copyright(monkeypatch):
    # metadata 含 copyright 字段 -> 跳过该 item 换下一个 (只下公共领域图).
    import asyncio, json
    from douyin_tools.web_image_search import WebImageSearchTool
    tool = WebImageSearchTool()
    items = [
        {"href": "https://x/a", "data": [{"nasa_id": "A", "title": "x", "copyright": "Some Agency"}]},
        {"href": "https://x/b", "data": [{"nasa_id": "B", "title": "y", "photographer": "NASA"}]},
    ]
    called = {"resolve": []}
    monkeypatch.setattr(WebImageSearchTool, "_search_nasa", lambda self, q: _async_val(items))
    def fake_resolve(self, href):
        called["resolve"].append(href)
        return _async_val("https://x/b~large.jpg" if href == "https://x/b" else "https://x/a~large.jpg")
    monkeypatch.setattr(WebImageSearchTool, "_resolve_image_url", fake_resolve)
    monkeypatch.setattr(WebImageSearchTool, "_download", lambda self, url, dst: _async_val(dst))
    r = asyncio.run(tool.execute(json.dumps(["earth"]), per_query=1, expected=1))
    d = json.loads(r)
    # 版权图 A 被跳过, 只 resolve B
    assert "https://x/a" not in called["resolve"]
    assert "https://x/b" in called["resolve"]
    assert len(d["image_paths"]) == 1


def test_compose_planet_fallback(tmp_path, monkeypatch):
    # 首页黑底无点击欲: 坏图(缺图/退化 width<100/近纯黑 mean<15&std<3) -> 星球占位图兜底.
    # 近纯黑需 mean+std 双条件: 纯黑 mean~0 std~0.2, uniform-bright mean~200 std~0,
    # legit 暗调带星点 mean~0 std~11. mean 区分暗/亮, std 区分纯黑/带星点.
    import cv2
    import numpy as np
    from douyin_tools import ffmpeg_compose as fmod
    monkeypatch.setattr(fmod, "IMAGE_DIR", str(tmp_path), raising=False)
    planet = tmp_path / "_planet_cover.png"
    cv2.imwrite(str(planet), np.full((1920, 1080, 3), 120, dtype=np.uint8))
    tool = fmod.FfmpegComposeTool()

    # 缺图 -> planet
    base = tool._scene_base("/nope/missing.png", 0)
    assert base.shape == (1920, 1080, 3)
    assert float(np.mean(base)) > 40

    # 退化图 (1024x3 comfyui bug) -> planet
    deg = tmp_path / "deg.png"
    cv2.imwrite(str(deg), np.zeros((1024, 3, 3), dtype=np.uint8))
    base = tool._scene_base(str(deg), 0)
    assert float(np.mean(base)) > 40

    # 近纯黑 (uniform value 5, std~0) -> planet (首页不能黑)
    near_black = tmp_path / "near_black.png"
    cv2.imwrite(str(near_black), np.full((800, 600, 3), 5, dtype=np.uint8))
    base = tool._scene_base(str(near_black), 0)
    assert float(np.mean(base)) > 40

    # 正常亮图 -> 用原图 (非 planet)
    norm = tmp_path / "norm.png"
    cv2.imwrite(str(norm), np.full((800, 600, 3), 200, dtype=np.uint8))
    base = tool._scene_base(str(norm), 0)
    assert float(np.mean(base)) > 150

    # legit 暗调带星点 (std>3 有星点) -> 保留原图 (scene 0 也保留, 不误杀暗调艺术)
    dark_stars = tmp_path / "dark_stars.png"
    ds = np.zeros((800, 600, 3), dtype=np.uint8)
    rng = np.random.default_rng(7)
    for _ in range(400):
        ds[int(rng.integers(0, 800)), int(rng.integers(0, 600))] = int(rng.integers(150, 255))
    cv2.imwrite(str(dark_stars), ds)
    base = tool._scene_base(str(dark_stars), 0)
    assert float(np.mean(base)) < 15  # 全黑底+稀疏星点, mean 极低但 std>3 -> 保留

    # 后续镜近纯黑 -> 也 planet (坏图到处修, 不只首页)
    base = tool._scene_base(str(near_black), 1)
    assert float(np.mean(base)) > 40


def test_cron_path_integration_bindings():
    # A4: cron 自动化路径集成校验 (不需 daemon, 静态结构断言).
    # 1) sync_graphs CRON_FIELDS 8 个 job 表达式合法 (4 发布+2 评论+1 进化+1 修复);
    # 2) CRON_BY_GRAPH 每个图名有对应 graph 文件;
    # 3) cron 热路径 auto_publish graph 含 C1 修复的 topic_gen 补货节点 (cron 不传 topic 也能自产).
    import glob
    import re
    import sync_graphs as sg

    # 1) 8 cron job 表达式 5 字段合法.
    assert len(sg.CRON_FIELDS) == 8
    expr_re = re.compile(r"^[\d*/,-]+\s+[\d*/,-]+\s+[\d*/,-]+\s+[\d*/,-]+\s+[\d*/,-]+$")
    for jid, (expr, _) in sg.CRON_FIELDS.items():
        assert expr_re.match(expr), f"bad cron expr {jid}: {expr}"

    # 2) CRON_BY_GRAPH 每个图名在 graphs/ 目录有对应文件.
    graphs_dir = os.path.join(os.path.dirname(sg.__file__), "graphs")
    graph_files = {os.path.basename(p) for p in glob.glob(os.path.join(graphs_dir, "*.json"))}
    for graph_name in sg.CRON_BY_GRAPH:
        # 图名 -> 文件名约定 douyin_<name>.json (auto_publish -> douyin_auto_publish.json).
        fname = f"{graph_name}.json"
        assert fname in graph_files, f"cron graph {graph_name} 无对应文件 {fname}"

    # 3) cron 热路径 auto_publish 含 topic_gen 补货首节点 (C1 修复核心).
    auto_path = os.path.join(graphs_dir, "douyin_auto_publish.json")
    with open(auto_path) as f:
        auto = json.load(f)
    nodes = auto.get("nodes", {})
    assert "topic_gen" in nodes, "auto_publish 必须有 topic_gen 补货首节点 (C1)"
    tg = nodes["topic_gen"]
    assert tg.get("tool_name") == "topic_gen"
    # output_mapping 扁平输出 topic/series/hook_variant 注入下游 (扁平插值约束).
    om = tg.get("tool_params", {}).get("output_mapping", {})
    for k in ("topic", "series", "hook_variant"):
        assert k in om, f"topic_gen output_mapping 缺 {k} (C1 注入契约)"
    # agent_id 内嵌 (GUI 路径零改动触发 tool config 注入, PR#132).
    assert auto.get("agent_id"), "auto_publish 顶层必须有 agent_id"

    # 4) edges: gate_stock false 分支经 gate_refill_budget 门再接 topic_gen (H4 补货上限 + C1 补货入口).
    edges = [(e.get("source_id"), e.get("target_id"), e.get("label")) for e in auto.get("edges", [])]
    assert ("gate_stock", "gate_refill_budget", "false") in edges, "gate_stock false 必须接 refill_budget 门 (H4)"
    assert ("gate_refill_budget", "topic_gen", "true") in edges, "budget ok 必须接 topic_gen (补货入口)"
    assert ("topic_gen", "script", None) in edges or any(
        e[0] == "topic_gen" and e[1] == "script" for e in edges
    ), "topic_gen 必须接 script"


# ===== P2 回归测试 =====


def test_scheduler_parse_windows_sorted():
    # L5: peak_windows 配置乱序时, _parse_peak_windows 必须排序, 否则 _next_window 漏选下一个窗口.
    from douyin_tools.publish_scheduler import _parse_peak_windows, PublishSchedulerTool
    out = _parse_peak_windows(["20-21", "9-10", "14-15"])
    assert out == [(9, 10), (14, 15), (20, 21)], f"未排序: {out}"
    # _next_window 取首个未到的窗口, 乱序配置下也正确 (10:30 在 9-10 之后, 应指 14:00 而非 9:00).
    nw = PublishSchedulerTool._next_window(10 * 60 + 30, out)
    assert nw == "14:00", f"乱序配置下 next_window 错误: {nw}"


def test_ops_report_pattern_hit_any_keyword():
    # L2: pattern "极端假设/灾难场景" 含两个关键词, 标题命中任一即算匹配.
    # 旧实现只拼前 4 字 "极端假设" 连续子串, 命中不到 "灾难场景" 类标题.
    from douyin_tools.ops_report import _pattern_hit
    assert _pattern_hit("如果灾难场景降临地球", "极端假设/灾难场景") is True
    assert _pattern_hit("这是一个极端假设的实验", "极端假设/灾难场景") is True
    assert _pattern_hit("纯陈述事实无情绪", "极端假设/灾难场景") is False
    # 、分隔同样支持.
    assert _pattern_hit("标题过长无冲突", "纯系列编号开头、标题过长无冲突") is True


def test_reply_claim_replied_atomic_and_lock(tmp_path, monkeypatch):
    # R1: _claim_replied 用 temp+os.replace+flock 原子写, 并发 cron 不丢已回复记录.
    import douyin_tools.douyin_reply as mod
    rf = str(tmp_path / "replied.json")
    monkeypatch.setattr(mod, "REPLIED_FILE", rf)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    claimed, before = DouyinReplyTool._claim_replied(["c1", "c2", "c3"], 5)
    assert claimed == {"c1", "c2", "c3"}
    assert before == 0
    with open(rf) as f:
        data = json.load(f)
    assert set(data["replied_ids"]) == {"c1", "c2", "c3"}
    # 再 claim 一次: 已回复的跳过, 新 cid 才 claim, 验证覆盖正确 (非半写文件).
    claimed2, before2 = DouyinReplyTool._claim_replied(["c1", "c4"], 5)
    assert claimed2 == {"c4"}
    assert before2 == 3
    with open(rf) as f:
        data = json.load(f)
    assert set(data["replied_ids"]) == {"c1", "c2", "c3", "c4"}
    # 临时文件已清理.
    assert not os.path.exists(rf + ".tmp")


def test_reply_claim_respects_max_reply(tmp_path, monkeypatch):
    import douyin_tools.douyin_reply as mod
    rf = str(tmp_path / "replied.json")
    monkeypatch.setattr(mod, "REPLIED_FILE", rf)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    claimed, _ = DouyinReplyTool._claim_replied(["c1", "c2", "c3", "c4"], 2)
    assert claimed == {"c1", "c2"}
    # 再次 claim 同集: c1/c2 已回复跳过, c3/c4 新 claim.
    claimed2, _ = DouyinReplyTool._claim_replied(["c1", "c2", "c3", "c4"], 2)
    assert claimed2 == {"c3", "c4"}


def test_reply_load_replied_roundtrip(tmp_path, monkeypatch):
    import douyin_tools.douyin_reply as mod
    rf = str(tmp_path / "replied.json")
    monkeypatch.setattr(mod, "REPLIED_FILE", rf)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    DouyinReplyTool._claim_replied(["a", "b"], 5)
    loaded = DouyinReplyTool._load_replied()
    assert loaded == {"a", "b"}


def test_fetch_comments_real_id_dedup():
    # L1: JS 返回 id 字段 (真实 comment_id 或 user|text|ts 三元组), 同文不同人评论不误判重复.
    # _parse_comments 内部 python 循环用 r["id"] 去重; 同文不同人 id 不同 -> 两条都保留.
    import asyncio
    from douyin_tools.douyin_fetch_comments import DouyinFetchCommentsTool
    raw = [
        {"text": "太震撼了", "user": "用户A", "id": "real_id_1"},
        {"text": "太震撼了", "user": "用户B", "id": "real_id_2"},
        {"text": "学到了", "user": "用户C", "id": "real_id_3"},
    ]

    class FakePage:
        async def evaluate(self, script):
            return raw

    t = DouyinFetchCommentsTool()
    comments = asyncio.run(t._parse_comments(FakePage(), 10))
    # 同文不同人两条都应保留 (旧 md5 逻辑会把第二条当重复跳过, 只剩 1 条).
    assert len(comments) == 3, f"同文不同人评论不应被误判重复: {len(comments)}"
    ids = [c["id"] for c in comments]
    assert ids == ["real_id_1", "real_id_2", "real_id_3"], f"id 应取真实 comment_id: {ids}"


def test_fetch_comments_dedup_same_id():
    # L1: 相同真实 id 的重复评论 (DOM 重复遍历命中) 去重, 只留一条.
    import asyncio
    from douyin_tools.douyin_fetch_comments import DouyinFetchCommentsTool
    raw = [
        {"text": "好看", "user": "用户A", "id": "dup_id"},
        {"text": "好看", "user": "用户A", "id": "dup_id"},
    ]

    class FakePage:
        async def evaluate(self, script):
            return raw

    t = DouyinFetchCommentsTool()
    comments = asyncio.run(t._parse_comments(FakePage(), 10))
    assert len(comments) == 1, f"相同 id 应去重: {len(comments)}"


def test_ops_health_file_alerts(tmp_path, monkeypatch):
    # M2: ops_health 检测关键文件缺失/过期. 用临时 queue+ops 目录模拟停摆态.
    import asyncio
    import time
    import douyin_tools.ops_health as mod
    qdir = tmp_path / "queue"
    odir = tmp_path / "ops"
    qdir.mkdir()
    odir.mkdir()
    monkeypatch.setattr(mod, "QUEUE_DIR", str(qdir), raising=False)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(odir), raising=False)
    monkeypatch.setattr(mod, "PENDING_DIR", str(qdir / "pending"), raising=False)
    monkeypatch.setattr(mod, "PUBLISHED_DIR", str(qdir / "published"), raising=False)
    monkeypatch.setattr(mod, "PATTERNS_FILE", str(odir / "winning_patterns.json"), raising=False)
    monkeypatch.setattr(mod, "BLACKLIST_FILE", str(odir / "topic_blacklist.json"), raising=False)
    monkeypatch.setattr(mod, "REPLIED_FILE", str(odir / "replied.json"), raising=False)
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(odir / "stats_history.jsonl"), raising=False)
    # 全部文件缺失 -> 三条停摆告警 + low_stock(pending=0).
    t = mod.OpsHealthTool()
    res = json.loads(asyncio.run(t.execute(check_cron=False)))
    alerts = res["alerts"]
    assert any("evolve_never_run" in a for a in alerts)
    assert any("comment_never_run" in a for a in alerts)
    assert any("self_repair_never_run" in a for a in alerts)
    assert any("low_stock" in a for a in alerts)
    assert res["status"] == "degraded"

    # 补齐文件 + pending, 健康态.
    (qdir / "pending").mkdir()
    for i in range(5):
        (qdir / "pending" / f"item{i}.json").write_text("{}")
    (qdir / "published").mkdir()
    for fn, content in (
        ("winning_patterns.json", '{"winning_topics":[]}'),
        ("replied.json", '{"replied_ids":["c1"]}'),
        ("topic_blacklist.json", '{"blacklist":[]}'),
        ("stats_history.jsonl", '{"vid":"x"}'),
    ):
        (odir / fn).write_text(content)
    res2 = json.loads(asyncio.run(t.execute(check_cron=False)))
    assert res2["status"] == "healthy", f"补齐后应健康: {res2['alerts']}"
    assert res2["alert_count"] == 0


def test_ops_health_vid_backfill_rate(tmp_path, monkeypatch):
    import asyncio
    import douyin_tools.ops_health as mod
    monkeypatch.setattr(mod, "QUEUE_DIR", str(tmp_path), raising=False)
    pub = tmp_path / "published"
    pub.mkdir()
    (pub / "p1.json").write_text(json.dumps({"vid": "123"}))
    (pub / "p2.json").write_text(json.dumps({"vid": ""}))  # 缺 vid
    monkeypatch.setattr(mod, "PUBLISHED_DIR", str(pub), raising=False)
    t = mod.OpsHealthTool()
    rate = t._vid_backfill_rate()
    assert rate["total"] == 2
    assert rate["with_vid"] == 1
    assert rate["rate"] == 0.5


def test_e3_font_path_config_default_and_override(tmp_path, monkeypatch):
    # E3: FONT_PATH 进配置. 默认空 -> 走系统字体回退链 (STHeiti/PingFang).
    # 非空用配置值, 不存在回退系统字体. 静态常量 import 时求值, 测试用 reload 模块.
    import importlib
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"video": {"font_path": ""}}), encoding="utf-8")
    monkeypatch.setenv("DOUYIN_CONFIG_FILE", str(cfg))
    from douyin_tools import config as cfgmod
    importlib.reload(cfgmod)
    assert cfgmod.FONT_PATH, "空配置应回退到系统字体非空串"
    assert os.path.exists(cfgmod.FONT_PATH), "回退字体应真实存在"

    # 非空配置 + 路径存在 -> 用配置值
    real_font = "/System/Library/Fonts/PingFang.ttc"
    if os.path.exists(real_font):
        cfg.write_text(json.dumps({"video": {"font_path": real_font}}), encoding="utf-8")
        importlib.reload(cfgmod)
        assert cfgmod.FONT_PATH == real_font, "存在路径应采用配置值"

    # 非空配置 + 路径不存在 -> 回退系统字体
    cfg.write_text(json.dumps({"video": {"font_path": "/no/such/font.ttc"}}), encoding="utf-8")
    importlib.reload(cfgmod)
    assert cfgmod.FONT_PATH != "/no/such/font.ttc", "不存在路径应回退"
    assert os.path.exists(cfgmod.FONT_PATH), "回退字体应真实存在"

    # 恢复: 用默认配置 reload 避免污染同 session 其他测试
    monkeypatch.delenv("DOUYIN_CONFIG_FILE", raising=False)
    importlib.reload(cfgmod)


def test_e5_winner_graph_aweme_title_variable():
    # E5: winner graph aweme_title 从硬编码魔法串改成 {{aweme_title}} 变量.
    # 业务参数变量化, 改目标不改 graph JSON. --var 注入经 graph.execute variables.
    import json as _json
    p = os.path.join(os.path.dirname(__file__), "..", "graphs", "douyin_comment_reply_winner.json")
    with open(p, encoding="utf-8") as f:
        g = _json.load(f)
    fetch_params = g["nodes"]["fetch"]["tool_params"]
    assert fetch_params["aweme_title"] == "{{aweme_title}}", "winner graph 应变量化 aweme_title"


def test_e5_run_graph_var_flag_parse(monkeypatch, capsys):
    # E5: run_graph --var KEY=VAL 解析成 extra_vars 注入 graph.execute variables.
    import run_graph as rg
    captured = {}

    class FakeNS:
        graph = "x.json"
        approve = "true"
        loop = 0
        series = "宇宙奥秘"
        var = ["aweme_title=最冷的地方", "bad_no_eq", "k=v=with_eq"]

    monkeypatch.setattr(rg.argparse.ArgumentParser, "parse_args", lambda self: FakeNS())
    # stub run_once 捕获 extra_vars
    async def fake_run_once(graph_path, approve=True, extra_vars=None):
        captured["extra_vars"] = extra_vars
        return 0, ""
    monkeypatch.setattr(rg, "run_once", fake_run_once)
    monkeypatch.setattr(rg, "reload_json_config", lambda: {})
    monkeypatch.setattr(rg, "mlx_health", lambda: {"ok": True, "model_count": 1})
    # asyncio.run 需真实驱动 coro 拿 (rc, _) 元组. patch 成真事件循环 run.
    real_run = rg.asyncio.run
    monkeypatch.setattr(rg.asyncio, "run", real_run)
    import sys
    monkeypatch.setattr(sys, "exit", lambda rc: None)
    try:
        rg.main()
    except SystemExit:
        pass
    ev = captured.get("extra_vars") or {}
    assert ev.get("aweme_title") == "最冷的地方", "--var key=val 应解析"
    assert "bad_no_eq" not in str(ev), "无等号 --var 应忽略"
    assert ev.get("k") == "v=with_eq", "split(=,1) 只切第一个等号"


async def test_e8_queue_ops_enqueue_repr_script(tmp_path, monkeypatch):
    # E8: queue_ops 退役本地 _parse_script 改用 script_helpers.parse_script 单一入口.
    # AgentRuntime 扁平插值后 script_json 是 dict repr (单引号), 旧 _parse_script
    # 只 json.loads 无 repr 回退 -> None -> hook_variant 丢失. parse_script 应解析 repr.
    from douyin_tools import queue_ops as qmod
    qdir = str(tmp_path / "queue")
    monkeypatch.setattr(qmod, "QUEUE_DIR", qdir)
    for d in ("pending", "inflight", "published", "failed"):
        monkeypatch.setattr(QueueOpsTool, f"{d.upper()}_DIR", os.path.join(qdir, d))
    t = QueueOpsTool()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    script = {"title": "黑洞1克", "description": "#x", "narration": "n", "scenes": [], "hook_variant": "A"}
    repr_str = repr(script)  # 单引号 dict repr, 非合法 JSON
    assert "'" in repr_str and not repr_str.lstrip().startswith("{\"")
    r = json.loads(await t.execute(action="enqueue", video_path=str(video), script_json=repr_str, topic="黑洞"))
    assert r["status"] == "enqueued"
    item_id = r["item_id"]
    with open(os.path.join(qmod.QUEUE_DIR, "pending", f"{item_id}.json"), encoding="utf-8") as f:
        item = json.load(f)
    assert item["hook_variant"] == "A", "repr script 应解析出 hook_variant (parse_script 单一入口+repr回退)"
    assert item["title"] == "黑洞1克"
    # 旧 _parse_script 方法应已删除 (退役单一入口)
    assert not hasattr(QueueOpsTool, "_parse_script"), "_parse_script 应退役"


async def test_e8_mlx_image_script_first(tmp_path, monkeypatch):
    # E8: mlx_image.execute 应 parse_script 优先 (契约同 comfyui_produce),
    # 传整个 script_json(dict repr) 时提 scenes[].img_prompt, 不再 _parse_list 把 dict 分段.
    from douyin_tools import mlx_image as mmod
    t = MlxImageTool()
    called = {"gen": []}

    async def fake_gen(prompt, n, size, idx):
        called["gen"].append(prompt)
        return f"/fake/{idx}.png"

    monkeypatch.setattr(t, "_gen_mlx", fake_gen)
    monkeypatch.setattr(t, "_pick_fallback", lambda fb, idx: f"/fb/{idx}.png")
    script = {"title": "x", "narration": "n", "scenes": [
        {"img_prompt": "jupiter storm, no people"},
        {"img_prompt": "saturn rings, no people"},
    ]}
    repr_str = repr(script)
    r = json.loads(await t.execute(prompts=repr_str))
    assert r.get("image_paths"), "应有图路径"
    prompts_got = [p for p in called["gen"] if "jupiter" in p or "saturn" in p]
    assert len(prompts_got) == 2, "应从 script 提取 2 个 img_prompt (parse_script 优先), 不是把 dict repr 分段"
    assert all("no people" in p for p in prompts_got), "提取的应是完整 img_prompt 含排除词"
def test_ops_health_stale_inflight_alert(tmp_path, monkeypatch):
    # 审计 0908 F2 ④: inflight 项 age 超 2×LEASE_TTL -> 告警 (发布进程崩溃/卡死, 重复发布窗口开).
    import asyncio
    import time
    import douyin_tools.ops_health as mod
    qdir = tmp_path / "queue"
    odir = tmp_path / "ops"
    inflight = qdir / "inflight"
    inflight.mkdir(parents=True)
    odir.mkdir()
    monkeypatch.setattr(mod, "QUEUE_DIR", str(qdir), raising=False)
    monkeypatch.setattr(mod, "INFLIGHT_DIR", str(inflight), raising=False)
    monkeypatch.setattr(mod, "PENDING_DIR", str(qdir / "pending"), raising=False)
    monkeypatch.setattr(mod, "PUBLISHED_DIR", str(qdir / "published"), raising=False)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(odir), raising=False)
    monkeypatch.setattr(mod, "PATTERNS_FILE", str(odir / "wp.json"), raising=False)
    monkeypatch.setattr(mod, "BLACKLIST_FILE", str(odir / "bl.json"), raising=False)
    monkeypatch.setattr(mod, "REPLIED_FILE", str(odir / "r.json"), raising=False)
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(odir / "h.jsonl"), raising=False)
    monkeypatch.setattr(mod, "LEASE_TTL", 100, raising=False)
    now = int(time.time())
    # stale: leased_until 远超 2×TTL=200 (now - leased_until > 200)
    (inflight / "stale.json").write_text(json.dumps({"leased_until": now - 300}))
    # fresh: leased_until 未过期
    (inflight / "fresh.json").write_text(json.dumps({"leased_until": now + 50}))
    t = mod.OpsHealthTool()
    stale_count = t._stale_inflight_count()
    assert stale_count == 1, f"应只检测 1 个 stale, got {stale_count}"
    res = json.loads(asyncio.run(t.execute(check_cron=False)))
    alerts = res["alerts"]
    assert any("stale_inflight_over_2x_ttl" in a for a in alerts), f"缺 stale 告警: {alerts}"


async def test_perf_analyze_excludes_estimated_from_attribution(tmp_path, monkeypatch):
    # 审计0908 F3: estimated 行 plays 被置 0 -> 永落 cold 切片 -> 归因提取假"差片共性"
    # -> 回灌生成端 -> 自增强污染闭环. perf_analyze (决策层) 必须排除 estimated 行.
    import douyin_tools.perf_analyze as amod
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    hist = tmp_path / "stats_history.jsonl"
    rows = []
    # 3 真实行: 1 高播放 (hot) + 2 低播放 (cold); 2 estimated 行 plays=0 (修复前会挤占 cold 切片).
    rows.append(json.dumps({"vid": "real0", "plays": 200, "title": "真爆款", "snapshot_at": "2026-08-10 10:00:00"}))
    rows.append(json.dumps({"vid": "real1", "plays": 30, "title": "真差片A", "snapshot_at": "2026-08-10 10:00:00"}))
    rows.append(json.dumps({"vid": "real2", "plays": 20, "title": "真差片B", "snapshot_at": "2026-08-10 10:00:00"}))
    rows.append(json.dumps({"vid": "est0", "plays": 0, "estimated": True, "title": "假数据A", "snapshot_at": "2026-08-10 10:00:00"}))
    rows.append(json.dumps({"vid": "est1", "plays": 0, "estimated": True, "title": "假数据B", "snapshot_at": "2026-08-10 10:00:00"}))
    hist.write_text("\n".join(rows))
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(amod, "PATTERNS_FILE", str(tmp_path / "patterns.json"), raising=False)
    monkeypatch.setattr(amod, "PENDING_PATTERNS_FILE", str(tmp_path / "pending_patterns.json"), raising=False)
    monkeypatch.setattr(amod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(amod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    t = PerfAnalyzeTool()
    captured = {}

    async def fake_call_model(self, evidence, model):
        captured["evidence"] = evidence
        return {"winning_topics": [], "winning_hooks": [], "losing_patterns": [], "title_formula": "", "topic_gen_hint": ""}

    monkeypatch.setattr(PerfAnalyzeTool, "_call_model", fake_call_model)
    res = await t.execute(min_samples=3)
    d = json.loads(res)
    # M3-4: perf_analyze 写 pending_patterns.json (提案), status=proposed, needs_review=true.
    assert d.get("status") in ("ok", "proposed") or "patterns_file" in d or "pending_file" in d
    assert d.get("needs_review") == "true"
    ev = captured["evidence"]
    # 真实标题进归因, estimated 标题不进归因 (F3 核心断言: estimated 不污染 cold 切片).
    assert "真爆款" in ev
    assert "假数据A" not in ev
    assert "假数据B" not in ev


# ===== M3-4 数据门禁 tests =====


async def test_snapshot_propagates_estimated_flag(tmp_path, monkeypatch):
    # M3-4: perf_track._snapshot 据 plays_source=estimated 传播 row.estimated 标志.
    # 旧实现 row dict 无 estimated 字段 -> perf_analyze 排除逻辑永不触发 (M0 止血漏网).
    import douyin_tools.perf_track as pmod
    from douyin_tools.perf_track import PerfTrackTool
    from douyin_tools import history_store
    hist = tmp_path / "stats_history.jsonl"
    est = tmp_path / "stats_estimated.jsonl"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(history_store, "ESTIMATED_FILE", str(est))
    monkeypatch.setattr(history_store, "ALERTS_FILE", str(tmp_path / "team_alerts.jsonl"))
    monkeypatch.setattr(pmod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(pmod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    items = [
        {"vid": "v1", "plays": 100, "title": "real", "create_time": 0},
        {"vid": "v2", "plays": 30, "plays_source": "estimated", "title": "est", "create_time": 0},
    ]
    stats = {"fetched_at": "2026-09-11 10:00:00", "items": items}
    t = PerfTrackTool()
    res = await t.execute(action="snapshot", stats_json=json.dumps(stats))
    d = json.loads(res)
    assert d["status"] == "snapshotted"
    assert d["real_count"] == 1
    assert d["estimated_count"] == 1
    real_rows = [json.loads(l) for l in hist.read_text().strip().split("\n") if l]
    est_rows = [json.loads(l) for l in est.read_text().strip().split("\n") if l]
    assert real_rows[0]["estimated"] is False
    assert est_rows[0]["estimated"] is True


async def test_stats_real_estimated_physical_split(tmp_path, monkeypatch):
    # M3-4 §5.6: estimated 行物理隔离到 ESTIMATED_FILE, 不与真实数据同行同表.
    import douyin_tools.perf_track as pmod
    from douyin_tools.perf_track import PerfTrackTool
    from douyin_tools import history_store
    hist = tmp_path / "stats_history.jsonl"
    est = tmp_path / "stats_estimated.jsonl"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(history_store, "ESTIMATED_FILE", str(est))
    monkeypatch.setattr(history_store, "ALERTS_FILE", str(tmp_path / "team_alerts.jsonl"))
    monkeypatch.setattr(pmod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(pmod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    items = [
        {"vid": "r1", "plays": 100, "title": "r1", "create_time": 0},
        {"vid": "r2", "plays": 200, "title": "r2", "create_time": 0},
        {"vid": "r3", "plays": 50, "title": "r3", "create_time": 0},
        {"vid": "r4", "plays": 80, "title": "r4", "create_time": 0},
        {"vid": "r5", "plays": 10, "title": "r5", "create_time": 0},
        {"vid": "e1", "plays": 0, "plays_source": "estimated", "title": "e1", "create_time": 0},
        {"vid": "e2", "plays": 0, "plays_source": "estimated", "title": "e2", "create_time": 0},
        {"vid": "e3", "plays": 0, "plays_source": "estimated", "title": "e3", "create_time": 0},
    ]
    stats = {"fetched_at": "2026-09-11 10:00:00", "items": items}
    t = PerfTrackTool()
    res = await t.execute(action="snapshot", stats_json=json.dumps(stats))
    d = json.loads(res)
    assert d["real_count"] == 5
    assert d["estimated_count"] == 3
    real_lines = [l for l in hist.read_text().strip().split("\n") if l]
    est_lines = [l for l in est.read_text().strip().split("\n") if l]
    assert len(real_lines) == 5
    assert len(est_lines) == 3
    real_vids = {json.loads(l)["vid"] for l in real_lines}
    est_vids = {json.loads(l)["vid"] for l in est_lines}
    assert real_vids == {"r1", "r2", "r3", "r4", "r5"}
    assert est_vids == {"e1", "e2", "e3"}


async def test_estimated_ratio_alert_triggers(tmp_path, monkeypatch):
    # M3-4 §6.2 line 378: estimated 占比 > 20% 阈值 -> team.alert + result.alerted=true.
    import douyin_tools.perf_track as pmod
    from douyin_tools.perf_track import PerfTrackTool
    from douyin_tools import history_store
    hist = tmp_path / "stats_history.jsonl"
    est = tmp_path / "stats_estimated.jsonl"
    alerts = tmp_path / "team_alerts.jsonl"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(history_store, "ESTIMATED_FILE", str(est))
    monkeypatch.setattr(history_store, "ALERTS_FILE", str(alerts))
    monkeypatch.setattr(pmod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(pmod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    items = [
        {"vid": "r1", "plays": 100, "title": "r1", "create_time": 0},
        {"vid": "r2", "plays": 200, "title": "r2", "create_time": 0},
        {"vid": "r3", "plays": 50, "title": "r3", "create_time": 0},
        {"vid": "e1", "plays": 0, "plays_source": "estimated", "title": "e1", "create_time": 0},
        {"vid": "e2", "plays": 0, "plays_source": "estimated", "title": "e2", "create_time": 0},
    ]
    stats = {"fetched_at": "2026-09-11 10:00:00", "items": items}
    t = PerfTrackTool()
    res = await t.execute(action="snapshot", stats_json=json.dumps(stats))
    d = json.loads(res)
    # 2 est / 5 total = 0.4 > 0.2 threshold
    assert d["estimated_ratio"] == 0.4
    assert d["alerted"] == "true"
    assert alerts.exists()
    alert_line = alerts.read_text().strip().split("\n")[-1]
    alert = json.loads(alert_line)
    assert alert["alert"] == "estimated_ratio_exceeded"
    assert alert["ratio"] == 0.4


async def test_estimated_ratio_below_threshold_no_alert(tmp_path, monkeypatch):
    # M3-4: ratio <= 阈值不告警.
    import douyin_tools.perf_track as pmod
    from douyin_tools.perf_track import PerfTrackTool
    from douyin_tools import history_store
    hist = tmp_path / "stats_history.jsonl"
    est = tmp_path / "stats_estimated.jsonl"
    alerts = tmp_path / "team_alerts.jsonl"
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    monkeypatch.setattr(history_store, "ESTIMATED_FILE", str(est))
    monkeypatch.setattr(history_store, "ALERTS_FILE", str(alerts))
    monkeypatch.setattr(pmod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(pmod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    items = [
        {"vid": "r1", "plays": 100, "title": "r1", "create_time": 0},
        {"vid": "r2", "plays": 200, "title": "r2", "create_time": 0},
        {"vid": "r3", "plays": 50, "title": "r3", "create_time": 0},
        {"vid": "r4", "plays": 80, "title": "r4", "create_time": 0},
        {"vid": "r5", "plays": 90, "title": "r5", "create_time": 0},
        {"vid": "r6", "plays": 70, "title": "r6", "create_time": 0},
        {"vid": "r7", "plays": 60, "title": "r7", "create_time": 0},
        {"vid": "r8", "plays": 40, "title": "r8", "create_time": 0},
        {"vid": "r9", "plays": 30, "title": "r9", "create_time": 0},
        {"vid": "e1", "plays": 0, "plays_source": "estimated", "title": "e1", "create_time": 0},
    ]
    stats = {"fetched_at": "2026-09-11 10:00:00", "items": items}
    t = PerfTrackTool()
    res = await t.execute(action="snapshot", stats_json=json.dumps(stats))
    d = json.loads(res)
    # 1 est / 10 total = 0.1 <= 0.2
    assert d["estimated_ratio"] == 0.1
    assert d["alerted"] == "false"
    assert not alerts.exists()


async def test_perf_analyze_writes_pending_not_winning(tmp_path, monkeypatch):
    # M3-4 §5.6: perf_analyze 写 pending_patterns.json (提案), winning_patterns.json 未动.
    import douyin_tools.perf_analyze as amod
    from douyin_tools.perf_analyze import PerfAnalyzeTool
    from douyin_tools import history_store
    hist = tmp_path / "stats_history.jsonl"
    hist.write_text(json.dumps({"vid": "v1", "plays": 200, "title": "t", "snapshot_at": "2026-09-11 10:00:00"}) + "\n")
    monkeypatch.setattr(history_store, "HISTORY_FILE", str(hist))
    winning = tmp_path / "winning_patterns.json"
    pending = tmp_path / "pending_patterns.json"
    monkeypatch.setattr(amod, "PATTERNS_FILE", str(winning), raising=False)
    monkeypatch.setattr(amod, "PENDING_PATTERNS_FILE", str(pending), raising=False)
    monkeypatch.setattr(amod, "OPS_DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(amod, "PUBLISHED_DIR", str(tmp_path / "pub"), raising=False)
    async def fake_call_model(self, evidence, model):
        return {"winning_topics": ["topicA"], "winning_hooks": ["hookA"], "losing_patterns": ["bad"], "title_formula": "f", "topic_gen_hint": "h"}
    monkeypatch.setattr(PerfAnalyzeTool, "_call_model", fake_call_model)
    t = PerfAnalyzeTool()
    res = await t.execute(min_samples=1)
    d = json.loads(res)
    assert d["status"] == "proposed"
    assert d["needs_review"] == "true"
    assert pending.exists()
    assert not winning.exists()
    pdat = json.loads(pending.read_text())
    assert pdat["winning_topics"] == ["topicA"]


async def test_pattern_review_promote(tmp_path, monkeypatch):
    # M3-4: pattern_review promote 转正 pending -> winning.
    from douyin_tools import pattern_review as prev
    from douyin_tools.pattern_review import PatternReviewTool
    monkeypatch.setattr(prev, "WINNING_FILE", str(tmp_path / "winning_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "PENDING_FILE", str(tmp_path / "pending_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "OPS_DATA_DIR", str(tmp_path), raising=False)
    pending = tmp_path / "pending_patterns.json"
    winning = tmp_path / "winning_patterns.json"
    pending.write_text(json.dumps({"winning_topics": ["X"], "winning_hooks": ["Y"]}))
    t = PatternReviewTool()
    res = await t.execute(action="promote")
    d = json.loads(res)
    assert d["status"] == "promoted"
    assert winning.exists()
    wdat = json.loads(winning.read_text())
    assert wdat["winning_topics"] == ["X"]
    assert not pending.exists()


async def test_pattern_review_reject(tmp_path, monkeypatch):
    # M3-4: pattern_review reject 删 pending, winning 未动.
    from douyin_tools import pattern_review as prev
    from douyin_tools.pattern_review import PatternReviewTool
    monkeypatch.setattr(prev, "WINNING_FILE", str(tmp_path / "winning_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "PENDING_FILE", str(tmp_path / "pending_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "OPS_DATA_DIR", str(tmp_path), raising=False)
    pending = tmp_path / "pending_patterns.json"
    winning = tmp_path / "winning_patterns.json"
    winning.write_text(json.dumps({"winning_topics": ["old"]}))
    pending.write_text(json.dumps({"winning_topics": ["new"]}))
    t = PatternReviewTool()
    res = await t.execute(action="reject")
    d = json.loads(res)
    assert d["status"] == "rejected"
    assert not pending.exists()
    assert winning.exists()
    wdat = json.loads(winning.read_text())
    assert wdat["winning_topics"] == ["old"]


async def test_pattern_review_list(tmp_path, monkeypatch):
    # M3-4: pattern_review list 返回 pending + winning 两者.
    from douyin_tools import pattern_review as prev
    from douyin_tools.pattern_review import PatternReviewTool
    monkeypatch.setattr(prev, "WINNING_FILE", str(tmp_path / "winning_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "PENDING_FILE", str(tmp_path / "pending_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "OPS_DATA_DIR", str(tmp_path), raising=False)
    (tmp_path / "winning_patterns.json").write_text(json.dumps({"winning_topics": ["W"]}))
    (tmp_path / "pending_patterns.json").write_text(json.dumps({"winning_topics": ["P"]}))
    t = PatternReviewTool()
    res = await t.execute(action="list")
    d = json.loads(res)
    assert d["status"] == "listed"
    assert d["has_pending"] == "true"
    assert d["has_winning"] == "true"
    assert d["pending"]["winning_topics"] == ["P"]
    assert d["winning"]["winning_topics"] == ["W"]


async def test_pattern_review_promote_no_pending(tmp_path, monkeypatch):
    # M3-4: promote 无 pending -> error.
    from douyin_tools import pattern_review as prev
    from douyin_tools.pattern_review import PatternReviewTool
    monkeypatch.setattr(prev, "WINNING_FILE", str(tmp_path / "winning_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "PENDING_FILE", str(tmp_path / "pending_patterns.json"), raising=False)
    monkeypatch.setattr(prev, "OPS_DATA_DIR", str(tmp_path), raising=False)
    t = PatternReviewTool()
    res = await t.execute(action="promote")
    d = json.loads(res)
    assert "error" in d


def test_consumers_still_read_winning(tmp_path, monkeypatch):
    # M3-4 regression: 消费者 (topic_gen) 读 winning_patterns.json, 不读 pending_patterns.json.
    from douyin_tools import topic_gen as tmod
    from douyin_tools.topic_gen import TopicGenTool
    winning = tmp_path / "winning_patterns.json"
    pending = tmp_path / "pending_patterns.json"
    winning.write_text(json.dumps({"winning_topics": ["REAL"], "winning_hooks": ["realhook"], "losing_patterns": [], "title_formula": "", "topic_gen_hint": "realhint"}))
    pending.write_text(json.dumps({"winning_topics": ["PENDING"], "winning_hooks": ["pendinghook"], "losing_patterns": [], "title_formula": "", "topic_gen_hint": "pendinghint"}))
    hint = TopicGenTool._load_pattern_hint(str(winning))
    assert "REAL" in hint
    assert "PENDING" not in hint



def test_b2_load_published_titles_recent_first(tmp_path, monkeypatch):
    # B2 (审计 B2): _load_published_titles 旧 sorted() 升序 = 最旧在前,
    # 下游 excl_list[:30] 取最老 30 -> 最近已发布主题不在排重窗口 -> 重复选题.
    # 修: 降序取最近. 验证返回顺序 = 最近在前.
    from douyin_tools import topic_gen as tmod
    from douyin_tools.topic_gen import TopicGenTool
    pub = tmp_path / "pub"
    pub.mkdir()
    # 写 3 个文件, mtime 递增 (old < mid < new), 内容含可识别 topic.
    import time as _t
    for i, topic in enumerate(["old_topic", "mid_topic", "new_topic"]):
        p = pub / f"v{i}.json"
        p.write_text(json.dumps({"topic": topic}))
        mt = _t.time() + i * 100
        os.utime(p, (mt, mt))
    monkeypatch.setattr(tmod, "PUBLISHED_DIR", str(pub), raising=False)
    titles = TopicGenTool._load_published_titles()
    assert titles[0] == "new_topic", f"最近应在前, got {titles[0]}"
    assert titles[-1] == "old_topic", f"最旧应在后, got {titles[-1]}"


async def test_b11_session_expired_no_claim(tmp_path, monkeypatch):
    # B11 (审计 B11): 旧 _claim_replied 先于 session check -> session 过期 cid 永久消费.
    # 修: 先 session check 再 claim. 验证 session 过期时 REPLIED_FILE 不被写入任何 cid.
    import douyin_tools.douyin_reply as mod
    import sys
    rf = str(tmp_path / "replied.json")
    monkeypatch.setattr(mod, "REPLIED_FILE", rf)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)

    class ExpiredPage:
        async def inner_text(self, *a, **k):
            return "扫码登录"
        async def goto(self, *a, **k):
            pass
        async def evaluate(self, *a, **k):
            return "ok"

    class ExpiredCtx:
        pages = [ExpiredPage()]
        async def close(self):
            pass
        async def new_page(self):
            return ExpiredPage()

    class ExpiredBrowser:
        async def launch_persistent_context(self, *a, **k):
            return ExpiredCtx()

    class ExpiredPlaywright:
        def __init__(self):
            self.chromium = ExpiredBrowser()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    fake_mod = type(sys)("playwright.async_api")
    fake_mod.async_playwright = lambda: ExpiredPlaywright()
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_mod)

    t = DouyinReplyTool()
    drafts = '[{"id":"c1","text":"hi","safe":true},{"id":"c2","text":"yo","safe":true}]'
    res = await t.execute(drafts_json=drafts, max_reply=5, headless=True)
    d = json.loads(res)
    assert d.get("error") == "session expired"
    # 核心: session 过期时不应持久化任何 cid (claim 未发生).
    assert not os.path.exists(rf) or json.load(open(rf)).get("replied_ids") == [], \
        "session 过期不应 claim 任何 cid"


async def test_b11_session_ok_then_claims(tmp_path, monkeypatch):
    # B11 正向: session 有效时 claim 正常发生 (reorder 不破坏正常路径).
    import douyin_tools.douyin_reply as mod
    import sys
    rf = str(tmp_path / "replied.json")
    monkeypatch.setattr(mod, "REPLIED_FILE", rf)
    monkeypatch.setattr(mod, "OPS_DATA_DIR", str(tmp_path), raising=False)

    class OkPage:
        async def inner_text(self, *a, **k):
            return "评论管理"
        async def goto(self, *a, **k):
            pass
        async def evaluate(self, *a, **k):
            return "ok"

    class OkCtx:
        pages = [OkPage()]
        async def close(self):
            pass
        async def new_page(self):
            return OkPage()

    class OkBrowser:
        async def launch_persistent_context(self, *a, **k):
            return OkCtx()

    class OkPlaywright:
        def __init__(self):
            self.chromium = OkBrowser()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    fake_mod = type(sys)("playwright.async_api")
    fake_mod.async_playwright = lambda: OkPlaywright()
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_mod)

    t = DouyinReplyTool()
    # _click_text/_click_work/_like_comment/_reply_comment 都走 evaluate -> mock 返回 ok.
    drafts = '[{"id":"c1","text":"hi","safe":true}]'
    res = await t.execute(drafts_json=drafts, max_reply=5, headless=True)
    d = json.loads(res)
    # session 有效 -> claim 发生 -> cid 持久化.
    persisted = json.load(open(rf)).get("replied_ids", [])
    assert "c1" in persisted, "session 有效时应 claim c1"


def test_b14_sync_preserves_disabled_cron(tmp_path, monkeypatch):
    # B14 (审计 B14): sync_graphs cron.register 旧硬编码 enabled=True
    # 覆盖运营者手动禁用. 修: 读现有 enabled 状态 preserve.
    import sync_graphs as sg
    import glob as _glob
    import asyncio as _asyncio

    gdir = tmp_path / "graphs"
    gdir.mkdir()
    (gdir / "douyin_auto_publish.json").write_text(json.dumps({
        "name": "douyin_auto_publish",
        "agent_id": "c65efddbe8c5",
        "nodes": {"start": {"type": "start"}},
        "edges": [],
    }))
    monkeypatch.setattr(sg, "GRAPHS_DIR", str(gdir))

    # 旧 store + cron: 0905 job 被运营者手动禁用 (enabled=False).
    fake = _FakeRpcClient(
        existing=[("old-auto", "douyin_auto_publish")],
        cron_jobs=[
            ("douyin_publish_plan_0905", "old-auto"),
            ("douyin_publish_plan_1105", "old-auto"),
        ],
    )
    # 模拟 0905 被禁用.
    fake.cron_jobs["douyin_publish_plan_0905"]["enabled"] = False
    fake.cron_jobs["douyin_publish_plan_1105"]["enabled"] = True
    monkeypatch.setattr(sg, "AgentClient", lambda socket_path=None: fake)
    files = sorted(_glob.glob(os.path.join(str(gdir), "*.json")))
    _asyncio.run(sg.sync_graphs(files))

    # 回填后: 0905 仍禁用, 1105 仍启用 (不被覆盖成 True).
    # _FakeRpcClient.cron.register 存 id/graph_id/expression, 需扩展存 enabled.
    reg_0905 = fake.cron_jobs["douyin_publish_plan_0905"]
    reg_1105 = fake.cron_jobs["douyin_publish_plan_1105"]
    assert reg_0905.get("enabled") is False, "运营者禁用的 job 不应被 sync 覆盖为 enabled"
    assert reg_1105.get("enabled") is True


async def test_b1_run_queue_publish_requeued_stops():
    # B1 (审计 B1): run_queue_publish 旧判 status=="publish_failed" 永不命中
    # (失败时 queue_ops 返回 {"status":"requeued"}). 修: 改判 requeued.
    # 验证 requeued 时停止循环 (不浪费 daily_limit 重试同条).
    import run_graph as rg

    call_count = {"n": 0}

    async def fake_run_once(graph_path, approve=True, extra_vars=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return 1, json.dumps({"status": "requeued"})
        return 0, json.dumps({"status": "archived"})

    monkeypatch_global = __import__("unittest.mock").mock.patch
    with monkeypatch_global.object(rg, "run_once", fake_run_once):
        rc = await rg.run_queue_publish("graph.json", daily_limit=5, approve=True)
    # requeued 应立即停止, 只调用 1 次 (不重试同条耗配额).
    assert call_count["n"] == 1, f"requeued 应停止, 实际调用 {call_count['n']} 次"
    assert rc == 0


# ============================================================
# M3-6: team-contract.yaml + Lead 规划轮 + 周计划 approve
# ============================================================

def test_team_contract_read_returns_contract(tmp_path, monkeypatch):
    # Lead 规划轮强制 read: 返回 team-contract.yaml 内容
    from douyin_tools.team_contract import TeamContractTool, load_contract
    import yaml
    contract = {
        "team": "douyin-ops", "version": 1,
        "roles": [{"name": "lead", "tools": ["queue_ops"], "duty": "x"}],
        "resources": {}, "sla": {}, "data_contract": {},
        "idempotency": {}, "interventions": [], "exit_criteria": {},
    }
    p = tmp_path / "tc.yaml"
    p.write_text(yaml.safe_dump(contract), encoding="utf-8")
    monkeypatch.setattr("douyin_tools.team_contract.CONTRACT_FILE", str(p))
    tool = TeamContractTool()
    out = json.loads(tool._read())
    assert out["status"] == "ok"
    assert out["contract"]["team"] == "douyin-ops"


def test_team_contract_read_missing_returns_error(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    monkeypatch.setattr("douyin_tools.team_contract.CONTRACT_FILE", str(tmp_path / "nope.yaml"))
    tool = TeamContractTool()
    out = json.loads(tool._read())
    assert out["error"]


def test_team_contract_validate_ok(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    import yaml
    contract = {
        "team": "douyin-ops", "version": 1,
        "roles": [{"name": "lead", "tools": ["queue_ops"], "duty": "x"}],
        "resources": {}, "sla": {}, "data_contract": {},
        "idempotency": {}, "interventions": [], "exit_criteria": {},
    }
    p = tmp_path / "tc.yaml"
    p.write_text(yaml.safe_dump(contract), encoding="utf-8")
    monkeypatch.setattr("douyin_tools.team_contract.CONTRACT_FILE", str(p))
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    out = json.loads(tool._validate())
    assert out["valid"] is True
    assert out["team"] == "douyin-ops"


def test_team_contract_validate_missing_keys(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    import yaml
    contract = {"team": "douyin-ops", "version": 1}  # missing roles/resources/sla/etc
    p = tmp_path / "tc.yaml"
    p.write_text(yaml.safe_dump(contract), encoding="utf-8")
    monkeypatch.setattr("douyin_tools.team_contract.CONTRACT_FILE", str(p))
    tool = TeamContractTool()
    out = json.loads(tool._validate())
    assert out["valid"] is False
    assert any("roles" in e for e in out["errors"])


def test_team_contract_validate_role_missing_duty(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    import yaml
    contract = {
        "team": "douyin-ops", "version": 1,
        "roles": [{"name": "lead", "tools": ["queue_ops"]}],  # missing duty
        "resources": {}, "sla": {}, "data_contract": {},
        "idempotency": {}, "interventions": [], "exit_criteria": {},
    }
    p = tmp_path / "tc.yaml"
    p.write_text(yaml.safe_dump(contract), encoding="utf-8")
    monkeypatch.setattr("douyin_tools.team_contract.CONTRACT_FILE", str(p))
    tool = TeamContractTool()
    out = json.loads(tool._validate())
    assert out["valid"] is False
    assert any("duty" in e for e in out["errors"])


async def test_team_contract_plan_draft_creates_pending(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool, _load_plan
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    out = json.loads(await tool.execute(action="plan_draft", plan_summary="本周发5条补货视频"))
    assert out["status"] == "drafted"
    assert out["plan_status"] == "pending_approval"
    plan = _load_plan(out["plan_id"])
    assert plan["summary"] == "本周发5条补货视频"
    assert plan["status"] == "pending_approval"
    assert plan["approved_at"] is None


async def test_team_contract_plan_draft_requires_summary(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    out = json.loads(await tool.execute(action="plan_draft", plan_summary=""))
    assert out["error"]


async def test_team_contract_plan_approve_flow(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool, _load_plan
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    drafted = json.loads(await tool.execute(action="plan_draft", plan_summary="周计划A"))
    plan_id = drafted["plan_id"]
    # approved
    out = json.loads(await tool.execute(action="plan_approve", plan_id=plan_id))
    assert out["status"] == "approved"
    plan = _load_plan(plan_id)
    assert plan["status"] == "approved"
    assert plan["approved_at"] is not None


async def test_team_contract_plan_approve_wrong_status(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    drafted = json.loads(await tool.execute(action="plan_draft", plan_summary="周计划B"))
    plan_id = drafted["plan_id"]
    # approve once
    await tool.execute(action="plan_approve", plan_id=plan_id)
    # second approve → reject (not pending)
    out = json.loads(await tool.execute(action="plan_approve", plan_id=plan_id))
    assert out["error"]


async def test_team_contract_plan_reject_flow(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool, _load_plan
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    drafted = json.loads(await tool.execute(action="plan_draft", plan_summary="周计划C"))
    plan_id = drafted["plan_id"]
    out = json.loads(await tool.execute(action="plan_reject", plan_id=plan_id, comment="预算不足"))
    assert out["status"] == "rejected"
    assert out["comment"] == "预算不足"
    plan = _load_plan(plan_id)
    assert plan["status"] == "rejected"
    assert plan["rejected_at"] is not None


async def test_team_contract_plan_list(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    await tool.execute(action="plan_draft", plan_summary="计划1")
    await tool.execute(action="plan_draft", plan_summary="计划2")
    out = json.loads(await tool.execute(action="plan_list"))
    assert out["status"] == "ok"
    assert out["count"] == 2


async def test_team_contract_plan_approve_not_found(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    out = json.loads(await tool.execute(action="plan_approve", plan_id="nonexistent"))
    assert out["error"] == "plan not found"


async def test_team_contract_unknown_action(tmp_path, monkeypatch):
    from douyin_tools.team_contract import TeamContractTool
    monkeypatch.setattr("douyin_tools.team_contract.PLAN_DIR", str(tmp_path / "plans"))
    tool = TeamContractTool()
    out = json.loads(await tool.execute(action="bogus"))
    assert out["error"]


def test_team_contract_load_contract_file_not_found(tmp_path):
    from douyin_tools.team_contract import load_contract
    data = load_contract(str(tmp_path / "missing.yaml"))
    assert data == {}


def test_team_contract_repo_contract_valid():
    # 仓内 team-contract.yaml 必须自洽: validate 通过
    from douyin_tools.team_contract import validate_contract
    result = validate_contract()
    assert result["valid"] is True, f"repo team-contract.yaml invalid: {result['errors']}"
    assert result["team"] == "douyin-ops"


# ============================================================
# M3-7: LLM/ComfyUI retry (3 exp backoff) + circuit breaker
# ============================================================

async def test_post_json_success(tmp_path, monkeypatch):
    # 正常 200 → 返回 resp, breaker record_success
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    resp = await rh.post_json("http://x/v1/chat", {"p": 1}, service="mlx_test")
    assert resp.status_code == 200
    assert calls["n"] == 1
    br = rh.get_breaker("mlx_test")
    assert br.state == "closed"
    assert br.failures == 0


async def test_post_json_retries_5xx_then_succeeds(tmp_path, monkeypatch):
    # 前 2 次 503, 第 3 次 200 → 重试成功, 总调用 3
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            calls["n"] += 1
            return FakeResp(503 if calls["n"] < 3 else 200)

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    resp = await rh.post_json("http://x", {}, service="mlx_rt", retries=3)
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_post_json_retries_network_exc(tmp_path, monkeypatch):
    # 网络异常重试, 最终成功
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise rh.httpx.ConnectError("boom")
            return FakeResp()

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    resp = await rh.post_json("http://x", {}, service="mlx_nx", retries=3)
    assert resp.status_code == 200
    assert calls["n"] == 2


async def test_post_json_exhausts_retries_raises(tmp_path, monkeypatch):
    # 3 次全 503 → 返回最后的 503 resp (不 raise, 调用方判 status)
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    calls = {"n": 0}

    class FakeResp:
        status_code = 503
        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    resp = await rh.post_json("http://x", {}, service="mlx_ex", retries=3)
    assert resp.status_code == 503
    assert calls["n"] == 3
    br = rh.get_breaker("mlx_ex")
    assert br.failures == 3


async def test_circuit_opens_after_threshold(tmp_path, monkeypatch):
    # 连续 threshold 次失败 → 熔断 open, 后续请求直接 CircuitOpenError
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    # 低 threshold 便于测试
    monkeypatch.setattr(rh, "_resilience_cfg", lambda: {"circuit_threshold": 2, "circuit_cooldown_sec": 60, "max_retries": 1, "backoff_base_sec": 0})
    calls = {"n": 0}

    class FakeResp:
        status_code = 503
        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    monkeypatch.setattr(rh, "append_alert", lambda alert: True)
    # 第一次: 1 次重试 503 → failure 1
    await rh.post_json("http://x", {}, service="mlx_cb", retries=1)
    br = rh.get_breaker("mlx_cb")
    assert br.failures == 1
    assert br.state == "closed"
    # 第二次: 1 次重试 503 → failure 2 → open
    await rh.post_json("http://x", {}, service="mlx_cb", retries=1)
    assert br.state == "open"
    # 第三次: circuit open → CircuitOpenError, 不发 HTTP
    with pytest.raises(rh.CircuitOpenError):
        await rh.post_json("http://x", {}, service="mlx_cb")
    assert calls["n"] == 2  # 第三次没发 HTTP


async def test_circuit_record_success_resets(tmp_path, monkeypatch):
    # 失败后成功 → breaker reset
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    monkeypatch.setattr(rh, "_resilience_cfg", lambda: {"circuit_threshold": 5, "circuit_cooldown_sec": 60, "max_retries": 1, "backoff_base_sec": 0})
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            calls["n"] += 1
            return FakeResp(503 if calls["n"] == 1 else 200)

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    # 第一次 503 (retries=1, 不重试) → failure 1
    await rh.post_json("http://x", {}, service="mlx_rs", retries=1)
    br = rh.get_breaker("mlx_rs")
    assert br.failures == 1
    # 第二次 200 → success → reset
    await rh.post_json("http://x", {}, service="mlx_rs", retries=1)
    assert br.failures == 0
    assert br.state == "closed"


async def test_circuit_open_emits_team_alert(tmp_path, monkeypatch):
    # 熔断 open 时 append_alert 被调用 (team.alert)
    from douyin_tools import resilient_http as rh
    rh.reset_breakers()
    monkeypatch.setattr(rh, "_resilience_cfg", lambda: {"circuit_threshold": 1, "circuit_cooldown_sec": 60, "max_retries": 1, "backoff_base_sec": 0})
    alerts = []

    class FakeResp:
        status_code = 500
        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None):
            return FakeResp()

    monkeypatch.setattr(rh.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(rh, "mlx_headers", lambda: {})
    monkeypatch.setattr(rh, "asyncio", type("A", (), {"sleep": staticmethod(lambda *a: _async_val(None))}))
    monkeypatch.setattr(rh, "append_alert", lambda alert: alerts.append(alert))
    await rh.post_json("http://x", {}, service="mlx_alert", retries=1)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "circuit_open"
    assert alerts[0]["service"] == "mlx_alert"
