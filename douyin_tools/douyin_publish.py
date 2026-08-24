import asyncio
import json
import logging
import os
import re
import signal
import subprocess

from tools.base import BaseTool

from .config import DOUYIN_PROFILE, DOUYIN_UPLOAD_URL, DOUYIN_MANAGE_URL

logger = logging.getLogger(__name__)

SEL_FILE_INPUT = "input[type='file']"
SEL_DESC_EDITOR = '[contenteditable="true"][data-placeholder="添加作品简介"]'
SEL_COVER_BLOCK = ".cover-Jg3T4p"
SEL_FRAME_ITEM = ".preview-frame-rt7Mc1"


class DouyinPublishTool(BaseTool):
    name = "douyin_publish"
    description = "发布一条抖音视频；dry_run=True 仅走到发布前不真发。需 playwright async api"
    parameters = {
        "video_path": {"type": "string", "description": "视频绝对路径"},
        "description": {"type": "string", "description": "作品描述含#话题"},
        "title": {"type": "string", "description": "作品标题，可空"},
        "dry_run": {"type": "boolean", "description": "True=不真发，仅准备"},
        "headless": {"type": "boolean", "description": "是否无头，默认 False"},
    }

    async def execute(
        self,
        video_path,
        description,
        title="",
        dry_run=True,
        headless=False,
        **_,
    ):
        dry_run = self._parse_bool(dry_run, default=True)
        headless = self._parse_bool(headless, default=False)
        if not os.path.exists(video_path):
            logger.error("video not exist %s", video_path)
            return json.dumps({"error": "video not exist"})
        from .script_helpers import parse_script, extract_description, extract_title
        desc_script = parse_script(description)
        if desc_script:
            description = extract_description(desc_script) or description
        title_script = parse_script(title)
        if title_script:
            title = extract_title(title_script) or title
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed")
            return json.dumps({"error": "playwright not installed"})

        result = {"dry_run": dry_run}
        try:
            async with async_playwright() as p:
                ctx = await self._launch_with_retry(p, headless)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                before = await self._get_work_count(page)
                logger.info("publish before_count=%d", before)
                # R6: 发布前抓 vid 快照, 传给 _publish -> _capture_new_vid 做 post/pre diff.
                # 并发 cron/手动发片时 batch[0] 可能是别人新作品, diff 才精确定位刚发的.
                pre_vids = await self._fetch_vid_list(page)
                logger.info("publish pre_vids snapshot count=%d", len(pre_vids))
                ok = await self._publish(
                    page, video_path, description, title, dry_run, before, pre_vids
                )
                await ctx.close()
            dry_ok = bool(ok.get("ok")) if isinstance(ok, dict) else bool(ok)
            if dry_run:
                result["status"] = "dry_run_ok" if dry_ok else "publish_failed"
                result["verify"] = dry_ok
                result["vid"] = ""
            else:
                result["status"] = "publish_ok" if dry_ok else "publish_failed"
                result["verify"] = dry_ok
                result["vid"] = (ok.get("vid") if isinstance(ok, dict) else "") or ""
            logger.info("douyin_publish done status=%s vid=%s", result["status"], result.get("vid"))
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.exception("douyin_publish failed")
            return json.dumps({"error": str(e)})

    @staticmethod
    def _kill_stale_profile_chrome():
        # 清残留 Testing chrome (同 user-data-dir=douyin_profile).
        # 前台/上次 publish 残留进程会占用 profile 独占锁 ->
        # launch_persistent_context 报 TargetClosedError ("正在现有的浏览器会话中打开").
        # 只清用 DOUYIN_PROFILE 的 Testing chrome, 不动用户日常 chrome.
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", "Google Chrome for Testing"],
                stderr=subprocess.DEVNULL,
            )
            pids = [int(x) for x in out.split() if x]
        except subprocess.CalledProcessError:
            return 0
        killed = 0
        for pid in pids:
            try:
                cmd = subprocess.check_output(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
            except subprocess.CalledProcessError:
                continue
            if DOUYIN_PROFILE not in cmd:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
                logger.warning("killed stale profile chrome pid=%d", pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                logger.warning("no permission to kill chrome pid=%d", pid)
        if killed:
            try:
                os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG)
            except ChildProcessError:
                pass
        return killed

    async def _launch_with_retry(self, p, headless, retries=1):
        # launch_persistent_context 失败 (TargetClosedError/profile 占用) 时
        # 清残留 chrome 后重试一次, 保证后台 cron 不被前台 profile 锁阻断.
        last_err = None
        for attempt in range(retries + 1):
            self._kill_stale_profile_chrome()
            try:
                ctx = await p.chromium.launch_persistent_context(
                    DOUYIN_PROFILE, headless=headless
                )
                if attempt > 0:
                    logger.info("launch succeeded on retry %d", attempt)
                return ctx
            except Exception as e:
                last_err = e
                msg = str(e)
                if attempt < retries and ("TargetClosedError" in type(e).__name__ or "closed" in msg.lower()):
                    logger.warning("launch failed (attempt %d): %s, retry after cleanup", attempt + 1, msg[:80])
                    await asyncio.sleep(2)
                    continue
                raise
        raise last_err

    @staticmethod
    def _parse_bool(val, default=False):
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        s = str(val).strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
        return default

    async def _publish(self, page, video_path, description, title, dry_run, before_count, pre_vids=None):
        logger.info("[1/10] open upload page")
        await page.goto(DOUYIN_UPLOAD_URL, timeout=20000)
        await asyncio.sleep(5)
        if "扫码登录" in await page.inner_text("body"):
            logger.warning("session expired, wait for scan login")
            await page.wait_for_url(
                lambda u: "creator.douyin.com" in u and "login" not in u,
                timeout=300000,
            )
            await asyncio.sleep(3)
            await page.goto(DOUYIN_UPLOAD_URL, timeout=20000)
            await asyncio.sleep(5)

        logger.info("[2/10] upload %s", video_path)
        await page.locator(SEL_FILE_INPUT).first.set_input_files(video_path)

        logger.info("[3/10] wait upload done")
        if not await self._wait_text(page, "作品描述", 180):
            logger.error("upload timeout")
            return False
        await asyncio.sleep(3)

        logger.info("[4/10] fill description")
        await self._fill_description(page, description)
        await asyncio.sleep(2)

        logger.info("[5/10] vertical cover 3:4")
        await self._open_cover_editor(page)
        await asyncio.sleep(2)
        await self._select_cover_frame(page)
        await asyncio.sleep(2)

        logger.info("[6/10] horizontal cover 4:3")
        await self._click_by_text(page, "设置横封面")
        await asyncio.sleep(2)
        await self._select_cover_frame(page)
        await asyncio.sleep(2)

        logger.info("[7/10] apply cover")
        await self._click_by_text(page, "完成")
        await asyncio.sleep(3)

        if dry_run:
            logger.info("DRY RUN stop before publish")
            return {"ok": True, "vid": ""}

        logger.info("[8/10] click publish")
        click_res = await self._click_publish(page)
        await asyncio.sleep(3)

        logger.info("[9/10] handle confirm dialog")
        confirmed = await self._handle_confirm(page, 15)
        if not confirmed:
            logger.error("publish aborted by leave/stash dialog, skip verify")
            return False
        await asyncio.sleep(5)

        logger.info("[10/10] verify by count")
        ok = await self._verify_by_count(page, before_count, 90)
        # C6 修: count-verify 失败 (before<0 或超时) 不能直接判失败丢弃.
        # count 基线未知时 count 路径无效, 但发布可能其实成功了 -> 抓 vid 兜底验证:
        # _capture_new_vid 调 item/list API 取首条 item_id_plain (最新作品=刚发的),
        # 拿到非空 vid = 新作品确实在抖音存在 = 发布真成功. 空才判失败留 pending 重试.
        vid = ""
        if ok:
            vid = await self._capture_new_vid(page, pre_vids)
            logger.info("captured new vid=%s", vid)
        else:
            vid = await self._capture_new_vid(page, pre_vids)
            if vid:
                ok = True
                logger.info("count-verify failed but vid captured=%s -> publish confirmed by vid", vid)
            else:
                logger.error("verify failed both count and vid, publish unconfirmed")
        return {"ok": ok, "vid": vid}

    async def _fill_description(self, page, text):
        await page.evaluate(
            f"""(() => {{
            const ed = document.querySelector('{SEL_DESC_EDITOR}') ||
                       document.querySelector('[contenteditable="true"]');
            if (!ed) return 'editor not found';
            ed.focus();
            ed.innerHTML = '';
            ed.textContent = '';
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, {text!r});
            ed.dispatchEvent(new InputEvent('input', {{bubbles: true, data: ed.textContent}}));
            return 'filled';
        }})()"""
        )

    async def _open_cover_editor(self, page):
        await page.evaluate(
            f"""(() => {{
            const c = document.querySelector('{SEL_COVER_BLOCK}');
            if (!c) return 'cover block not found';
            c.click();
            return 'clicked';
        }})()"""
        )

    async def _select_cover_frame(self, page):
        frames = page.locator(SEL_FRAME_ITEM)
        n = await frames.count()
        if n > 0:
            # 首页缩略图 = 封面. 选首帧 (fs[0]) = 视频第一帧 = 场景0.
            # 场景0 经 ffmpeg_compose._scene_base 兜底保证非黑 (星球占位图),
            # 选 mid 会命中后续暗调/退化分镜 -> 封面黑底无点击欲.
            idx = 0
            await page.evaluate(
                f"""(() => {{
                const fs = document.querySelectorAll('{SEL_FRAME_ITEM}');
                if (!fs.length) return 'no frames';
                const i = Math.min({idx}, fs.length - 1);
                fs[i].click();
                return 'clicked ' + fs.length;
            }})()"""
            )
            await asyncio.sleep(1)

    async def _click_by_text(self, page, text, tag="button"):
        await page.evaluate(
            f"""(() => {{
            const els = [...document.querySelectorAll('{tag},span,a,div')];
            const el = els.find(e => e.textContent.trim() === {text!r} || e.textContent.includes({text!r}));
            if (el) {{ el.click(); return 'clicked'; }}
            return 'not found';
        }})()"""
        )

    async def _wait_text(self, page, text, timeout=180):
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if text in await page.inner_text("body"):
                return True
            await asyncio.sleep(2)
        return False

    async def _click_publish(self, page):
        res = await page.evaluate(
            """(() => {
            const btns = [...document.querySelectorAll('button')];
            const primary = btns.find(b => b.textContent.trim() === '发布'
                && /primary-/.test(b.className));
            if (primary) { primary.click(); return 'primary'; }
            const plain = btns.find(b => b.textContent.trim() === '发布');
            if (plain) { plain.click(); return 'plain'; }
            return 'not found';
        })()"""
        )
        logging.info("click_publish result=%s", res)
        return res

    async def _handle_confirm(self, page, timeout=15):
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            body = await page.inner_text("body")
            if "离开" in body or "暂存" in body:
                logging.error("publish aborted: leave/stash dialog appeared (wrong button clicked)")
                return False
            await asyncio.sleep(1)
        return True

    async def _get_work_count(self, page):
        try:
            await page.goto(DOUYIN_MANAGE_URL, timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            body = await page.inner_text("body")
            m = re.search(r"共\s*(\d+)\s*个", body)
            if m:
                return int(m.group(1))
        except Exception as e:
            logger.warning("get_work_count failed: %s", e)
        return -1

    async def _verify_by_count(self, page, before, timeout=150):
        if before < 0:
            # C6 修: count 基线未知 (manage 页加载失败/正则未命中) 时不能靠 count diff 验证.
            # 旧实现无条件 return True -> 失败发布被误判成功 -> gate_ok -> 误归档 published ->
            # A/B variant 归因污染 + stats/snapshot 虚高 + self_repair 扫不到真实 vid.
            # count 未知 = 无法验证 = 判失败, 留 pending 重试 (调用方 _publish 走 vid 回退兜底).
            logger.warning("before_count unknown, count-verify impossible -> fail")
            return False
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        last = -1
        while loop.time() < end:
            now = await self._get_work_count(page)
            if now != last:
                logger.info("verify poll before=%d now=%d", before, now)
                last = now
            if now >= before + 1:
                logger.info("verify ok before=%d now=%d", before, now)
                return True
            await asyncio.sleep(8)
        logger.error("verify timeout before=%d last=%d", before, last)
        return False

    async def _fetch_vid_list(self, page):
        # 调 item/list API 返回当前作品 vid 有序集合 (最新在前).
        # R6: _capture_new_vid 用 pre/post diff 精确定位刚发的作品, 不再盲取 batch[0]
        # (并发 cron/手动发片时 batch[0] 可能是别人的新作品).
        try:
            await page.goto(DOUYIN_MANAGE_URL, timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            result = await page.evaluate(
                """async () => {
                const r = await fetch('https://creator.douyin.com/aweme/v1/creator/item/list?cursor=&aid=2906&limit=20', {
                    credentials: 'include', headers: {'Accept':'application/json'}
                });
                return JSON.stringify(await r.json());
            }"""
            )
            data = json.loads(result)
            batch = data.get("item_info_list", [])
            vids = [(b.get("item_id_plain") or "").strip() for b in batch]
            vids = [v for v in vids if v]
            return vids
        except Exception as e:
            logger.warning("fetch_vid_list failed: %s", e)
            return []

    async def _capture_new_vid(self, page, pre_vids=None):
        # R6: post-list 与 pre_vids (发布前快照) diff, 新增 vid = 刚发作品.
        # pre_vids 为空/None (兼容旧调用) 回退 batch[0]. diff 为空也回退 batch[0] (单发无并发).
        try:
            post_vids = await self._fetch_vid_list(page)
            if not post_vids:
                logger.warning("capture_new_vid empty item list")
                return ""
            if pre_vids:
                pre_set = set(pre_vids)
                new_vids = [v for v in post_vids if v not in pre_set]
                if new_vids:
                    logger.info("capture_new_vid diff found new vid=%s (pre=%d post=%d)",
                                new_vids[0], len(pre_set), len(post_vids))
                    return new_vids[0]
                logger.warning("capture_new_vid diff empty, fallback batch[0] (pre=%d post=%d)",
                               len(pre_set), len(post_vids))
            vid = post_vids[0]
            return vid
        except Exception as e:
            logger.warning("capture_new_vid failed: %s", e)
            return ""
