import asyncio
import json
import logging
import re

from tools.base import BaseTool

from .config import DOUYIN_COMMENT_URL
from .playwright_profile import profile_lock, launch_with_retry

logger = logging.getLogger(__name__)

SEL_SELECT_WORK = "选择作品"
SEL_SIDE_SHEET = ".douyin-creator-interactive-sidesheet-body"


class DouyinFetchCommentsTool(BaseTool):
    name = "douyin_fetch_comments"
    description = "从抖音评论管理页抓取最新评论列表（Playwright async，DOM 解析）"
    roles = frozenset({"researcher"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "aweme_title": {"type": "string", "description": "作品标题关键词，空=取最近作品"},
        "limit": {"type": "integer", "description": "最多取几条评论，默认 5"},
        "headless": {"type": "boolean", "description": "是否无头，默认 False"},
        "dry_run": {"type": "boolean", "description": "True=不打开浏览器，返回空列表（验证用）"},
    }

    async def execute(
        self,
        aweme_title="",
        limit=5,
        headless=False,
        dry_run=False,
        **_,
    ):
        if dry_run:
            logger.info("douyin_fetch_comments dry_run -> empty")
            return json.dumps({"comments": [], "count": 0, "has_new": False}, ensure_ascii=False)
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed")
            return json.dumps({"error": "playwright not installed"})
        try:
            with profile_lock():
                async with async_playwright() as p:
                    ctx = await launch_with_retry(p, headless)
                    # 审计 P3-16: 旧 _fetch 内 session 过期 return [] 后直接 await ctx.close,
                    # 但 _fetch 抛异常 (非 session 过期) 会跳过 ctx.close -> profile 锁泄漏.
                    # 改 try-finally 统一保证 close.
                    try:
                        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                        comments = await self._fetch(page, aweme_title, limit)
                    finally:
                        try:
                            await ctx.close()
                        except Exception as e:
                            logger.warning("fetch_comments ctx.close failed: %s", e)
            has_new = len(comments) > 0
            logger.info("douyin_fetch_comments done count=%d has_new=%s", len(comments), has_new)
            return json.dumps(
                {"comments": comments, "count": len(comments), "has_new": has_new},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("douyin_fetch_comments failed")
            return json.dumps({"error": str(e)})

    async def _fetch(self, page, aweme_title, limit):
        logger.info("[fetch] open comment manage page")
        await page.goto(DOUYIN_COMMENT_URL, timeout=30000)
        await asyncio.sleep(8)
        if "扫码登录" in await page.inner_text("body"):
            logger.warning("session expired at fetch_comments")
            return []
        await self._click_text(page, SEL_SELECT_WORK)
        await asyncio.sleep(6)
        if aweme_title:
            clicked = await self._click_work(page, aweme_title)
            if not clicked:
                logger.warning("work not found by title=%s, fallback first", aweme_title)
                await self._click_first_work(page)
        else:
            await self._click_first_work(page)
        await asyncio.sleep(10)
        comments = await self._parse_comments(page, limit)
        return comments

    async def _click_text(self, page, text):
        await page.evaluate(
            f"""(() => {{
            const all = document.querySelectorAll('*');
            for (const el of all) {{
                if ((el.innerText||'').trim() === {text!r} && el.offsetParent !== null) {{
                    el.click(); return 'ok';
                }}
            }}
            return 'no';
        }})()"""
        )

    async def _click_work(self, page, keyword):
        return await page.evaluate(
            f"""(() => {{
            const root = document.querySelector('{SEL_SIDE_SHEET}') || document.body;
            const all = root.querySelectorAll('*');
            for (const el of all) {{
                const t = (el.innerText||'').replace(/\\s+/g,' ').trim();
                if (t.includes({keyword!r}) && t.length < 150 && el.offsetParent !== null) {{
                    el.click(); return true;
                }}
            }}
            return false;
        }})()"""
        )

    async def _click_first_work(self, page):
        await page.evaluate(
            f"""(() => {{
            const root = document.querySelector('{SEL_SIDE_SHEET}') || document.body;
            const all = root.querySelectorAll('*');
            for (const el of all) {{
                const t = (el.innerText||'').trim();
                if (t.length > 5 && t.length < 100 && el.offsetParent !== null &&
                    !t.includes('选择') && !t.includes('管理')) {{
                    el.click(); return 'ok';
                }}
            }}
            return 'no';
        }})()"""
        )

    async def _parse_comments(self, page, limit):
        raw = await page.evaluate(
            """(() => {
            const rows = [];
            const seen = new Set();
            const walk = (el, depth) => {
                if (depth > 12) return;
                const t = (el.innerText || '').trim();
                if (t && t.length > 3 && t.length < 160) {
                    const hasAction = [...el.querySelectorAll('*')].some(c =>
                        ['回复','点赞','删除','举报'].includes((c.innerText||'').trim()));
                    const hasTime = /昨天|刚刚|\\d{1,2}:\\d{2}|\\d+分钟前|\\d+天前/.test(t);
                    if (hasAction && hasTime) {
                        const parts = t.split(/\\n+/).map(s=>s.trim()).filter(Boolean);
                        let user = '', text = '';
                        for (let i=0;i<parts.length;i++){
                            const p = parts[i];
                            if (/^(回复|点赞|删除|举报|没有更多评论|全部评论|全部人群|最新发布|评论管理|发送)$/.test(p)) continue;
                            if (/^(昨天|刚刚|\\d{1,2}:\\d{2}|\\d+分钟前|\\d+天前)/.test(p)) break;
                            if (!user) user = p;
                            else if (!text) text = p;
                        }
                        text = text || user;
                        const key = user + '|' + text;
                        if (text && text.length < 140 && !seen.has(key)) {
                            seen.add(key);
                            // 抖音评论容器常有 data-comment-id / data-id 属性, 抓真实 id 做幂等去重.
                            // 旧实现用 text 的 md5 当 id, 同文不同人评论会被误判重复跳过 (修 L1).
                            let cid = '';
                            let p = el;
                            for (let i=0;i<5 && p;i++){
                                cid = p.getAttribute && (p.getAttribute('data-comment-id') || p.getAttribute('data-id') || p.getAttribute('data-aweme-id'));
                                if (cid) break;
                                p = p.parentElement;
                            }
                            let ts = '';
                            const tm = t.match(/(昨天|刚刚|\\d{1,2}:\\d{2}|\\d+分钟前|\\d+天前)/);
                            if (tm) ts = tm[0];
                            const fallId = cid || (user + '|' + text + '|' + ts);
                            rows.push({text, user, id: fallId});
                        }
                    }
                }
                for (const c of el.children) walk(c, depth + 1);
            };
            walk(document.body, 0);
            return rows.slice(0, 40);
        })()"""
        )
        comments = []
        seen_ids = set()
        for r in raw:
            text = r.get("text", "")
            # 审计 P3-17: 旧 "共" in text 误杀含"共"字的合法评论 (如"一共多少星球").
            # 改精确匹配统计行模板 "共N个评论"/"共N个作品", 不拦单字.
            if not text:
                continue
            if any(s in text for s in ["个作品", "选择作品", "高清发布"]) or re.match(r"^共\d+个?(评论|作品|回复)", text):
                continue
            # 审计 P1-19: 旧 fallback id = user+|+text 丢弃时间戳, 同人同文不同时间评论被误判重复跳过.
            # 保留 JS 侧已拼的 fallId (含 ts), 回退再补 ts 后缀.
            cid = r.get("id", "")
            if not cid:
                ts_match = re.search(r"(昨天|刚刚|\d{1,2}:\d{2}|\d+分钟前|\d+天前)", text)
                cid = r.get("user", "") + "|" + text + "|" + (ts_match.group(1) if ts_match else "")
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            comments.append({"id": cid, "text": text, "user": r.get("user", "")})
            if len(comments) >= limit:
                break
        return comments
