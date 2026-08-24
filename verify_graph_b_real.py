import asyncio
import json
import logging
import os
import sys

from playwright.async_api import async_playwright

from douyin_tools.config import DOUYIN_COMMENT_URL, DOUYIN_PROFILE
from douyin_tools.douyin_fetch_comments import DouyinFetchCommentsTool
from douyin_tools.mlx_reply_draft import MlxReplyDraftTool
from douyin_tools.douyin_reply import DouyinReplyTool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("verify_graph_b_real")


async def main():
    os.environ.pop("FUSION_MLX_API_KEY", None)
    fetcher = DouyinFetchCommentsTool()
    drafter = MlxReplyDraftTool()
    replier = DouyinReplyTool()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(DOUYIN_PROFILE, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        logger.info("[real] open comment manage page")
        await page.goto(DOUYIN_COMMENT_URL, timeout=30000)
        await asyncio.sleep(8)
        body = await page.inner_text("body")
        if "扫码登录" in body:
            logger.error("session expired")
            await ctx.close()
            return 1
        logger.info("[real] login ok, fetch comments")
        await fetcher._click_text(page, "选择作品")
        await asyncio.sleep(6)
        await fetcher._click_first_work(page)
        await asyncio.sleep(10)
        comments = await fetcher._parse_comments(page, 3)
        logger.info("[real] fetched comments=%d", len(comments))
        for c in comments:
            logger.info("  id=%s user=%s text=%s", c["id"], c.get("user"), c.get("text")[:40])
        if not comments:
            logger.info("[real] no comments, nothing to reply")
            await ctx.close()
            return 0
        comments_json = json.dumps(comments, ensure_ascii=False)
        dres = json.loads(await drafter.execute(comments_json=comments_json, account_role="科普博主"))
        if dres.get("error"):
            logger.error("draft error: %s", dres)
            await ctx.close()
            return 1
        drafts = dres["drafts"]
        logger.info("[real] drafts=%d", len(drafts))
        for d in drafts:
            logger.info("  id=%s safe=%s comment_text=%s reply=%s",
                        d.get("id"), d.get("safe"), d.get("comment_text", "")[:30], d.get("text", "")[:40])

        replied_set = replier._load_replied()
        safe_drafts = [d for d in drafts if d.get("safe") is True]
        todo = [d for d in safe_drafts if d.get("id") not in replied_set][:3]
        logger.info("[real] todo=%d replied_before=%d", len(todo), len(replied_set))
        replied = []
        skipped = 0
        liked = 0
        for d in todo:
            key = d.get("comment_text", "") or d.get("text", "")
            cid = d.get("id", "")
            if cid in replied_set:
                skipped += 1
                logger.info("[real] skip already-replied id=%s", cid)
                continue
            ok_like = await replier._like_comment(page, key)
            if ok_like:
                liked += 1
                logger.info("[real] liked comment id=%s", cid)
            await asyncio.sleep(1)
            ok = await replier._reply_comment(page, key, d.get("text", ""))
            if ok:
                replied.append(cid)
                replied_set.add(cid)
                logger.info("[real] REPLIED id=%s text=%s", cid, d.get("text", "")[:40])
                await asyncio.sleep(4)
            else:
                skipped += 1
                logger.warning("[real] reply failed id=%s", cid)
        await ctx.close()
    replier._save_replied(replied_set)
    logger.info("=== REAL RESULT replied=%d liked=%d skipped=%d ===", len(replied), liked, skipped)
    logger.info("replied ids: %s", replied)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
