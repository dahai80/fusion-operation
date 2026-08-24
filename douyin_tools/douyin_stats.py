import asyncio
import datetime
import json
import logging
import re

from tools.base import BaseTool

from .config import DOUYIN_PROFILE, DOUYIN_MANAGE_URL, DOUYIN_HOME_URL

logger = logging.getLogger(__name__)


class DouyinStatsTool(BaseTool):
    name = "douyin_stats"
    description = "从抖音创作者后台抓取运营统计：所有作品播放/赞/评/分享 + 账号粉丝/获赞（Playwright async）"
    parameters = {
        "aweme_id": {"type": "string", "description": "指定作品id，空=全部作品"},
        "headless": {"type": "boolean", "description": "是否无头，默认 True"},
        "dry_run": {"type": "boolean", "description": "True=不打开浏览器，返回空统计（验证用）"},
    }

    async def execute(
        self,
        aweme_id="",
        headless=True,
        dry_run=False,
        **_,
    ):
        if dry_run:
            logger.info("douyin_stats dry_run -> empty")
            return json.dumps(
                {"fetched_at": "", "account": {}, "items": []},
                ensure_ascii=False,
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed")
            return json.dumps({"error": "playwright not installed"})
        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    DOUYIN_PROFILE, headless=headless
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                logger.info("[stats] open manage page ensure login")
                await page.goto(DOUYIN_MANAGE_URL, timeout=30000)
                await asyncio.sleep(6)
                if "扫码登录" in await page.inner_text("body"):
                    logger.warning("session expired at stats")
                    await ctx.close()
                    return json.dumps({"error": "session expired"})
                items = await self._fetch_items(page)
                if aweme_id:
                    items = [i for i in items if i.get("vid") == aweme_id]
                acc = await self._fetch_account(page)
                await ctx.close()
            fetched_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info("douyin_stats done items=%d followers=%s", len(items), acc.get("followers_raw"))
            return json.dumps(
                {"fetched_at": fetched_at, "account": acc, "items": items},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("douyin_stats failed")
            return json.dumps({"error": str(e)})

    async def _fetch_items(self, page):
        items = []
        cursor = ""
        for _ in range(10):
            result = await page.evaluate(
                """async (c) => {
                const r = await fetch('https://creator.douyin.com/aweme/v1/creator/item/list?cursor='+c+'&aid=2906&limit=20', {
                    credentials: 'include', headers: {'Accept':'application/json'}
                });
                return JSON.stringify(await r.json());
            }""",
                cursor,
            )
            try:
                data = json.loads(result)
            except (ValueError, TypeError):
                logger.warning("item list parse failed")
                break
            batch = data.get("item_info_list", [])
            for v in batch:
                items.append({
                    "title": (v.get("title", "") or "")[:60],
                    "vid": v.get("item_id_plain", ""),
                    "plays": v.get("play_count", 0),
                    "likes": v.get("digg_count", 0),
                    "comments": v.get("comment_count", 0),
                    "shares": v.get("share_count", 0),
                    "create_time": v.get("create_time", 0),
                })
            cursor = data.get("cursor", "")
            if not data.get("has_more"):
                break
            await asyncio.sleep(1.5)
        await self._scroll_manage_page(page)
        page_stats = await self._fetch_page_stats(page)
        if page_stats:
            for it in items:
                st = self._match_page_stats(it.get("title", ""), page_stats)
                if st:
                    if st.get("plays") is not None:
                        it["plays"] = st["plays"]
                    if st.get("likes") is not None:
                        it["likes"] = st["likes"]
                    if st.get("shares") is not None:
                        it["shares"] = st["shares"]
                    if st.get("comments") is not None:
                        it["comments"] = st["comments"]
            logger.info("page stats enriched matched=%d/%d", sum(1 for it in items if self._match_page_stats(it.get("title",""), page_stats)), len(items))
        # 页面正文仍缺播放量时按评论数估算 (抖音行业点赞/评论比约 10:1, 播放/评论约 20:1, 取保守 15:1).
        # 标记 plays_source=estimated, 归因/排行时降权参考 (修 P4: API 只回 comment_count 上游限制).
        for it in items:
            if not it.get("plays") and it.get("comments"):
                it["plays"] = max(it["comments"] * 15, it.get("likes", 0) * 3)
                it["plays_source"] = "estimated"
        est = sum(1 for it in items if it.get("plays_source") == "estimated")
        if est:
            logger.info("estimated plays for %d items (api comment_count only)", est)
        return items

    async def _scroll_manage_page(self, page):
        # 滚到底: 持续滚直到 scrollHeight 连续 3 次不变 (真正加载完所有懒加载作品).
        # 旧实现只滚 8 次且首遇高度不变即停 -> 懒加载未完, ~44% 作品缺真实播放量 (修 P4).
        try:
            stable = 0
            prev_h = 0
            max_iter = 60
            for _ in range(max_iter):
                await page.mouse.wheel(0, 3000)
                await asyncio.sleep(1.0)
                h = await page.evaluate("document.body.scrollHeight")
                if h == prev_h:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                    prev_h = h
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(1.5)
            logger.info("manage page scrolled to bottom stable=%d final_h=%s", stable, prev_h)
        except Exception as e:
            logger.warning("scroll manage page failed: %s", e)

    async def _fetch_page_stats(self, page):
        try:
            text = await page.inner_text("body")
        except Exception as e:
            logger.warning("page stats inner_text failed: %s", e)
            return []
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        stats = []
        labels = {"播放": "plays", "点赞": "likes", "评论": "comments", "分享": "shares"}
        status_words = {"已发布", "未发布", "审核中", "已删除", "私密", "公开", "可见"}
        cur = None
        for l in lines:
            if l in labels:
                cur = labels[l]
                if cur == "plays":
                    stats.append({"title": "", "plays": 0, "likes": 0, "comments": 0, "shares": 0})
                continue
            if cur and self._is_num(l):
                if stats:
                    stats[-1][cur] = self._to_num(l)
                cur = None
                continue
            cur = None
            if l in status_words:
                continue
            if re.match(r"^\d+:\d{2}$", l):
                continue
            if len(l) > 15 and stats and not stats[-1].get("title"):
                stats[-1]["title"] = l[:60]
        logger.info("page stats parsed blocks=%d", len(stats))
        return [s for s in stats if s.get("title")]

    @staticmethod
    def _is_num(s):
        if not s:
            return False
        if s.isdigit():
            return True
        if s.endswith("万") or s.endswith("亿"):
            core = s[:-1]
            try:
                float(core)
                return True
            except ValueError:
                return False
        return False

    @staticmethod
    def _to_num(s):
        if s.isdigit():
            return int(s)
        if s.endswith("万"):
            try:
                return int(float(s[:-1]) * 10000)
            except ValueError:
                return 0
        if s.endswith("亿"):
            try:
                return int(float(s[:-1]) * 100000000)
            except ValueError:
                return 0
        try:
            return int(s)
        except ValueError:
            return 0

    @staticmethod
    def _match_page_stats(title, page_stats):
        if not title or not page_stats:
            return None
        for st in page_stats:
            ps = st.get("title", "")
            if not ps:
                continue
            n = min(len(ps), len(title), 12)
            if n > 0 and ps[:n] == title[:n]:
                return st
        return None

    async def _fetch_account(self, page):
        try:
            await page.goto(DOUYIN_HOME_URL, timeout=25000)
            await asyncio.sleep(6)
            text = await page.inner_text("body")
            followers = total_likes = None
            m = re.search(r"粉丝\s*([\d.]+[万]?)", text)
            if m:
                followers = m.group(1)
            m2 = re.search(r"获赞\s*([\d.]+[万]?)", text)
            if m2:
                total_likes = m2.group(1)
            return {"followers_raw": followers, "total_likes_raw": total_likes}
        except Exception as e:
            logger.warning("fetch_account failed: %s", e)
            return {"error": str(e)[:200]}
