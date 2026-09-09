import asyncio
import json
import logging
import os

from tools.base import BaseTool

from .config import DOUYIN_COMMENT_URL, OPS_DATA_DIR
from .playwright_profile import profile_lock, launch_with_retry

logger = logging.getLogger(__name__)

SEL_SELECT_WORK = "选择作品"
REPLIED_FILE = os.path.join(OPS_DATA_DIR, "replied.json")


class DouyinReplyTool(BaseTool):
    name = "douyin_reply"
    description = "批量回复抖音评论：逐条点赞+回复，跳过不安全草稿，已回复的幂等跳过（Playwright async）"
    roles = frozenset({"publisher", "analyst"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "drafts_json": {"type": "string", "description": "回复草稿 JSON [{id,text,safe}]"},
        "aweme_title": {"type": "string", "description": "作品标题关键词，空=最近作品"},
        "max_reply": {"type": "integer", "description": "最多回复几条，默认 5"},
        "headless": {"type": "boolean", "description": "是否无头，默认 False"},
        "dry_run": {"type": "boolean", "description": "True=不打开浏览器，仅返回计划"},
    }

    async def execute(
        self,
        drafts_json,
        aweme_title="",
        max_reply=5,
        headless=False,
        dry_run=False,
        **_,
    ):
        drafts = self._parse_drafts(drafts_json)
        if not drafts:
            logger.warning("douyin_reply empty drafts")
            return json.dumps({"error": "empty drafts"})
        safe_drafts = [d for d in drafts if d.get("safe") is True]
        candidate_ids = [d.get("id", "") for d in safe_drafts]
        if dry_run:
            # dry_run: 只读快照算 todo, 不持久化 claim (不消耗 cid).
            replied_set = self._load_replied()
            todo = [d for d in safe_drafts if d.get("id", "") not in replied_set][:max_reply]
            logger.info(
                "douyin_reply dry_run drafts=%d safe=%d todo=%d",
                len(drafts), len(safe_drafts), len(todo),
            )
            return json.dumps(
                {"dry_run": True, "todo": [d.get("id") for d in todo], "skipped": len(safe_drafts) - len(todo)},
                ensure_ascii=False,
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed")
            return json.dumps({"error": "playwright not installed"})
        # B11 (审计 B11): 旧实现 _claim_replied 先于浏览器 launch + session check.
        # session 过期时 cid 已持久化落盘 -> 永久消费 -> 永不回复 (下次 cron 跳过).
        # 改: 先 launch + 探测 session 有效, 再 claim. session 过期不消耗任何 cid.
        # claim 仍原子 (load+claim+save 同一把 LOCK_EX, 毫秒级, 消除 RMW 窗口, 审计 R1).
        # F1: profile_lock 串行化整个浏览器会话 (跨 launch->work->close), launch_with_retry 复用共享模块.
        ctx = None
        todo = []
        replied = []
        skipped = 0
        liked = 0
        try:
            with profile_lock():
                async with async_playwright() as p:
                    ctx = await launch_with_retry(p, headless)
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    logger.info("[reply] open comment manage page")
                    await page.goto(DOUYIN_COMMENT_URL, timeout=30000)
                    await asyncio.sleep(8)
                    if "扫码登录" in await page.inner_text("body"):
                        logger.warning("session expired at reply, claim aborted (no cid consumed)")
                        await ctx.close()
                        return json.dumps({"error": "session expired"})
                    # session 有效, 现在原子 claim.
                    claimed, replied_before = self._claim_replied(candidate_ids, max_reply)
                    todo = [d for d in safe_drafts if d.get("id", "") in claimed]
                    logger.info(
                        "douyin_reply drafts=%d safe=%d todo=%d replied_before=%d claimed=%d",
                        len(drafts), len(safe_drafts), len(todo), replied_before, len(claimed),
                    )
                    if not todo:
                        await ctx.close()
                        logger.info("douyin_reply no new todo after claim, done")
                        return json.dumps(
                            {"replied": [], "liked": 0, "skipped": 0, "todo": 0},
                            ensure_ascii=False,
                        )
                    # claimed 的 cid 已持久化 (幂等), reply 中途失败不回滚 (宁可漏回复不重复回复).
                    await self._click_text(page, SEL_SELECT_WORK)
                    await asyncio.sleep(6)
                    if aweme_title:
                        await self._click_work(page, aweme_title)
                        await asyncio.sleep(8)
                    for i, d in enumerate(todo):
                        key = d.get("comment_text", "") or d.get("text", "")
                        cid = d.get("id", "")
                        # 审计 P2-16: 旧单条评论 raise 中断整批, 已 claim 的 cid 永不重试.
                        # per-comment try/except, 失败记 skipped 继续, 整批不被一条带崩.
                        try:
                            ok_like = await self._like_comment(page, key)
                            if ok_like:
                                liked += 1
                            ok = await self._reply_comment(page, key, d.get("text", ""))
                            if ok:
                                replied.append(cid)
                                await asyncio.sleep(4)
                            else:
                                skipped += 1
                                logger.warning("reply failed id=%s text=%.30s", cid, key)
                        except Exception as ce:
                            skipped += 1
                            logger.warning("reply per-comment error id=%s: %s", cid, ce)
            logger.info("douyin_reply done replied=%d liked=%d skipped=%d", len(replied), liked, skipped)
            return json.dumps(
                {"replied": replied, "liked": liked, "skipped": skipped, "todo": len(todo)},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.exception("douyin_reply failed")
            return json.dumps({"error": str(e)})
        finally:
            # 审计 0906 P1-2: 异常路径不释放 ctx -> Chrome 持 profile 独占锁 -> 下次 launch TargetClosedError.
            # 统一 finally close, 对齐 douyin_publish/douyin_self_comment 模式.
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception as ce:
                    logger.warning("reply ctx.close on finally failed: %s", ce)

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
        await page.evaluate(
            f"""(() => {{
            const root = document.querySelector('.douyin-creator-interactive-sidesheet-body') || document.body;
            const all = root.querySelectorAll('*');
            for (const el of all) {{
                const t = (el.innerText||'').replace(/\\s+/g,' ').trim();
                if (t.includes({keyword!r}) && t.length < 150 && el.offsetParent !== null) {{
                    el.click(); return 'ok';
                }}
            }}
            return 'no';
        }})()"""
        )

    async def _like_comment(self, page, comment_text):
        if not comment_text:
            return False
        return await page.evaluate(
            f"""(() => {{
            const all = document.querySelectorAll('*');
            for (const el of all) {{
                const t = (el.innerText || '').trim();
                if (t && t.includes({comment_text!r}) && t.length < 200 && el.offsetParent !== null) {{
                    const likeBtn = [...el.querySelectorAll('*')].find(c =>
                        (c.innerText||'').trim() === '点赞' && c.offsetParent !== null);
                    if (likeBtn) {{ likeBtn.click(); return true; }}
                }}
            }}
            return false;
        }})()"""
        )

    async def _reply_comment(self, page, comment_text, text):
        if not comment_text:
            return False
        clicked = await page.evaluate(
            f"""(() => {{
            const all = document.querySelectorAll('*');
            for (const el of all) {{
                const t = (el.innerText || '').trim();
                if (t && t.includes({comment_text!r}) && t.length < 200 && el.offsetParent !== null) {{
                    const replyBtn = [...el.querySelectorAll('*')].find(c =>
                        (c.innerText||'').trim() === '回复' && c.offsetParent !== null);
                    if (replyBtn) {{ replyBtn.click(); return true; }}
                }}
            }}
            return false;
        }})()"""
        )
        if not clicked:
            return False
        await asyncio.sleep(3)
        await page.evaluate(
            f"""(() => {{
            const inputs = document.querySelectorAll('[contenteditable="true"]');
            for (const inp of inputs) {{
                if (inp.offsetParent !== null) {{
                    inp.focus();
                    inp.innerText = {text!r};
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    return 'filled';
                }}
            }}
            return 'no_input';
        }})()"""
        )
        await asyncio.sleep(2)
        sent = await page.evaluate(
            """(() => {
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if ((el.innerText||'').trim() === '发送' && el.offsetParent !== null) {
                    el.click(); return true;
                }
            }
            return false;
        })()"""
        )
        return bool(sent)

    @staticmethod
    def _parse_drafts(raw):
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict) and "drafts" in v:
                    return v["drafts"]
            except (ValueError, TypeError):
                pass
            try:
                import ast

                v = ast.literal_eval(str(raw))
                if isinstance(v, list):
                    return v
                if isinstance(v, dict) and "drafts" in v:
                    return v["drafts"]
            except (ValueError, SyntaxError):
                pass
        if isinstance(raw, dict) and "drafts" in raw:
            return raw["drafts"]
        return []

    @staticmethod
    def _claim_replied(candidate_ids, max_reply):
        # 原子抢占: 在同一把 LOCK_EX(REPLIED_FILE) 内 load 现有已回复集 -> 算出未回复 cid -> 合并入集 -> 原子落盘.
        # 锁同一文件 (非旧的 .lock 旁路文件), RMW 全程持锁 (毫秒级), 消除并发 cron (13:35/21:35 重叠) 的丢记录窗口 (审计 R1).
        # 返回 (claimed_set, replied_before_count). claimed 为本次新抢占的 cid (上限 max_reply), 即 todo 的 id 集.
        import fcntl
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        lock_path = REPLIED_FILE + ".lock"
        tmp_path = REPLIED_FILE + ".tmp"
        claimed = set()
        replied_before = 0
        with open(lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    with open(REPLIED_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    replied_set = set(data.get("replied_ids", []))
                except (FileNotFoundError, ValueError, TypeError):
                    replied_set = set()
                replied_before = len(replied_set)
                for cid in candidate_ids:
                    if not cid or cid in replied_set:
                        continue
                    if len(claimed) >= max_reply:
                        break
                    claimed.add(cid)
                    replied_set.add(cid)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({"replied_ids": sorted(replied_set)}, f, ensure_ascii=False)
                os.replace(tmp_path, REPLIED_FILE)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        logger.info(
            "claim_replied candidates=%d replied_before=%d claimed=%d total=%d",
            len(candidate_ids), replied_before, len(claimed), replied_before + len(claimed),
        )
        return claimed, replied_before

    @staticmethod
    def _load_replied():
        # 兼容旧调用方/测试: 只读快照 (无写). 生产路径已改 _claim_replied 原子抢占.
        import fcntl
        os.makedirs(OPS_DATA_DIR, exist_ok=True)
        try:
            with open(REPLIED_FILE, "r", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return set(data.get("replied_ids", []))
        except (FileNotFoundError, ValueError, TypeError):
            return set()
