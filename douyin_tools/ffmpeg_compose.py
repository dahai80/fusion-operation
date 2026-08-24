import asyncio
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from tools.base import BaseTool

from .config import FPS, H, W, FONT_PATH, IMAGE_DIR, VIDEO_DIR, ensure_dirs

logger = logging.getLogger(__name__)


class FfmpegComposeTool(BaseTool):
    name = "ffmpeg_compose"
    description = "图片+配音合成 9:16 视频：Ken Burns 微动效 + 字幕 + ffmpeg 拼接混音"
    parameters = {
        "image_paths": {"type": "string", "description": "图片路径列表(JSON 字符串或逗号分隔)"},
        "audio_path": {"type": "string", "description": "配音音频路径"},
        "scenes": {"type": "string", "description": "分镜 JSON 字符串 [{cn_text,scene},...]"},
        "out_name": {"type": "string", "description": "输出文件名(不含扩展)"},
        "title": {"type": "string", "description": "顶部大标题，可空"},
    }

    async def execute(
        self,
        image_paths,
        audio_path,
        scenes="[]",
        out_name="",
        title="",
        **_,
    ):
        imgs = self._parse_list(image_paths)
        if not imgs or not audio_path or not os.path.exists(audio_path):
            logger.error("compose invalid imgs=%s audio=%s", bool(imgs), audio_path)
            return json.dumps({"error": "invalid inputs"})
        from .script_helpers import (
            parse_script, extract_scenes, extract_title, extract_narration_segments,
        )
        script = parse_script(scenes)
        scenes_list = extract_scenes(script) if script else []
        if not scenes_list:
            try:
                scenes_list = json.loads(scenes) if isinstance(scenes, str) else scenes
            except (ValueError, TypeError):
                scenes_list = []
        if not isinstance(scenes_list, list):
            scenes_list = []
        # 旁白片段: 每镜对应朗读的那几句, 用于字幕随配音逐段显示 (音画同步).
        narration_segs = extract_narration_segments(script) if script else []
        if not title and script:
            title = extract_title(script)
        ensure_dirs()
        name = out_name or f"video_{int(time.time())}"
        out_path = os.path.join(VIDEO_DIR, f"{name}.mp4")
        try:
            dur = self._audio_duration(audio_path)
            # C4 修: _render 是同步阻塞 CPU+IO (cv2/ffmpeg subprocess, 单条 14-34s).
            # 直接在 asyncio 事件循环内调用会阻塞整个 daemon, cron 期间其他 graph.execute/RPC 全部卡死.
            # 丢线程池跑, 主事件循环保持可响应.
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, self._render, imgs, scenes_list, narration_segs, audio_path, dur, title, out_path)
            if not ok:
                return json.dumps({"error": "render failed"})
            logger.info("compose done path=%s dur=%.1f", out_path, dur)
            return json.dumps({"video_path": out_path, "duration": round(dur, 1)}, ensure_ascii=False)
        except Exception as e:
            logger.exception("compose failed")
            return json.dumps({"error": str(e)})

    @staticmethod
    def _parse_list(val):
        if isinstance(val, list):
            return val
        if not val:
            return []
        # 修 R5: dict(JSON/repr) 无已知 key 时显式失败返回 [], 不再 fall through 到 split(",").
        # 旧逻辑把整个 dict repr 按逗号分成 N 段垃圾路径. split 仅留给 json+ast 都失败的纯逗号串.
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for k in ("image_paths", "paths", "files"):
                    if isinstance(parsed.get(k), list):
                        return parsed[k]
                logger.warning("compose _parse_list dict no known key: %s", str(val)[:80])
                return []
        except (ValueError, TypeError):
            pass
        try:
            import ast

            parsed = ast.literal_eval(str(val))
            if isinstance(parsed, list):
                return [str(p) for p in parsed]
            if isinstance(parsed, dict):
                for k in ("image_paths", "paths", "files"):
                    if isinstance(parsed.get(k), list):
                        return [str(p) for p in parsed[k]]
                logger.warning("compose _parse_list ast dict no known key: %s", str(val)[:80])
                return []
        except (ValueError, SyntaxError):
            pass
        return [p.strip() for p in str(val).split(",") if p.strip()]

    @staticmethod
    def _audio_duration(path):
        try:
            import wave

            with wave.open(path, "rb") as wf:
                return wf.getnframes() / float(wf.getframerate())
        except Exception as e:
            # 旧实现 bare except 静默吞, 时长拿不到一律回退 15s -> 音画错位无日志难定位.
            # Qwen3-TTS 输出 24kHz PCM WAV, wave 能读; 读失败多半文件损坏/路径错, 记日志.
            logger.warning("audio_duration parse failed path=%s err=%s, fallback 15.0", path, e)
            return 15.0

    def _render(self, imgs, scenes, narration_segs, audio_path, dur, title, out_path):
        total_frames = max(int(dur * FPS), FPS)
        n_scenes = len(imgs)
        if n_scenes == 0:
            return False
        # 按旁白片段字数比例分配每镜帧数 (音画同步), 无片段则等分.
        weights = []
        for si in range(n_scenes):
            seg = narration_segs[si] if si < len(narration_segs) else ""
            weights.append(max(len(seg), 1))
        total_w = sum(weights)
        frame_counts = [max(int(total_frames * w / total_w), FPS) for w in weights]
        # 修正尾差, 保证帧数之和精确等于 total_frames.
        diff = total_frames - sum(frame_counts)
        if diff:
            frame_counts[-1] = max(frame_counts[-1] + diff, FPS)
        tmp = tempfile.mkdtemp(prefix="douyin_compose_")
        try:
            return self._render_loop(tmp, imgs, scenes, narration_segs, audio_path, total_frames, frame_counts, title, out_path)
        finally:
            # 清理临时帧目录 (单条视频数千帧 png + 中间 mp4, 不清则磁盘膨胀).
            shutil.rmtree(tmp, ignore_errors=True)
            logger.info("compose tmp cleaned %s", tmp)

    def _render_loop(self, tmp, imgs, scenes, narration_segs, audio_path, total_frames, frame_counts, title, out_path):
        # 每镜用 ffmpeg 从 stdin 读 raw frame 编码, 不再逐帧 cv2.imwrite 落盘 (修 P5: 旧实现每帧写 png + ffmpeg 再读回, 单视频数千次磁盘 IO 是 14-34s 主因).
        # Ken Burns + 字幕 + 顶部标题的 Python 渲染逻辑全部保留 (视觉效果不变, 风险低).
        scene_segs = []
        for si, img_path in enumerate(imgs):
            base = self._scene_base(img_path, si)
            nf = frame_counts[si]
            sub_text = ""
            if si < len(narration_segs) and narration_segs[si]:
                sub_text = narration_segs[si]
            elif si < len(scenes) and isinstance(scenes[si], dict):
                sub_text = scenes[si].get("cn_text", "")
            seg = os.path.join(tmp, f"sc_{si:03d}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-f", "rawvideo", "-pixel_format", "rgb24",
                "-video_size", f"{W}x{H}", "-framerate", str(FPS),
                "-i", "-",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-an", seg,
            ]
            # C5 修: stderr 收集 (旧 DEVNULL 吞掉 ffmpeg 报错, 编码失败无任何线索).
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            seg_ok = True
            try:
                for t in range(nf):
                    frame = self._ken_burns(base, t, nf)
                    if title:
                        frame = self._put_text(frame, title, W // 2, 160, 60, (255, 215, 0))
                    if sub_text:
                        frame = self._put_subtitle(frame, sub_text, 60, H - 260, W - 120)
                    proc.stdin.write(frame.tobytes())
                proc.stdin.close()
                rc = proc.wait(timeout=300)
                if rc != 0:
                    err_tail = proc.stderr.read().decode("utf-8", "replace")[-500:] if proc.stderr else ""
                    logger.error("scene encode rc=%d seg=%s stderr=%s", rc, seg, err_tail)
                    seg_ok = False
            except Exception as e:
                logger.error("scene encode failed seg=%s err=%s", seg, e)
                proc.kill()
                proc.wait()
                seg_ok = False
            # C5 修: 编码失败的 seg 不进 concat (旧实现无条件 append -> 坏 mp4 / 不存在文件进 concat demuxer
            # -> concat 报错或拼出残缺流, 但 check=False 不看 -> 最终 out_path 可能存在但是坏视频 -> 入库 -> 抖音黑屏).
            if seg_ok and os.path.exists(seg) and os.path.getsize(seg) > 0:
                scene_segs.append(seg)
            else:
                logger.error("skip bad/empty seg=%s ok=%s", seg, seg_ok)

        if not scene_segs:
            logger.error("all scene segs failed, abort compose out=%s", out_path)
            return False

        cl = os.path.join(tmp, "cl.txt")
        with open(cl, "w", encoding="utf-8") as f:
            for v in scene_segs:
                f.write(f"file '{v}'\n")
        no_audio = os.path.join(tmp, "combined.mp4")
        # C5 修: 检查 returncode + 记 stderr, 旧 check=False 静默 -> concat 失败仍继续混音.
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", cl,
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-an", no_audio],
            capture_output=True, timeout=600, check=False,
        )
        if r1.returncode != 0 or not os.path.exists(no_audio):
            logger.error("concat failed rc=%d stderr=%s", r1.returncode, r1.stderr.decode("utf-8", "replace")[-500:])
            return False
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", no_audio, "-i", audio_path,
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
             "-map", "0:v:0", "-map", "1:a:0", "-shortest", out_path],
            capture_output=True, timeout=600, check=False,
        )
        if r2.returncode != 0:
            logger.error("mux audio failed rc=%d stderr=%s", r2.returncode, r2.stderr.decode("utf-8", "replace")[-500:])
            return False
        # C5 修: 旧实现 return os.path.exists(out_path) 只看文件存在不看是否可播放.
        # ffmpeg 报错也可能留 0 字节 / 残缺文件. 加 size>0 基本校验.
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
            logger.error("output mp4 missing/too-small out=%s", out_path)
            return False
        return True

    @staticmethod
    def _planet_path():
        return os.path.join(IMAGE_DIR, "_planet_cover.png")

    def _scene_base(self, img_path, scene_idx):
        # 首页 = 视频首帧 = 抖音缩略图. 首镜黑底无点击欲 -> 用星球占位图兜底.
        # 精确判定坏图 (实测 out/images 1982 张):
        #   - 缺图: 文件不存在
        #   - 退化: width<100 (comfyui FusionImageGen 1024x3 bug, mean 1-2)
        #   - 近纯黑: mean<15 且 std<3 (模型偶出纯黑太空底 mean~0 std~0.2;
        #     与 legit 暗调带星点 mean~0 std~11 区分靠 std, 与 uniform-bright mean~200 std~0 区分靠 mean)
        # 首镜 (scene_idx==0) 强制兜底坏图 (封面必须非黑). 后续镜同样兜底坏图,
        # 保留正常暗调分镜艺术表达 (legit 暗太空带主体 std>=3 不触发).
        if os.path.exists(img_path):
            img = cv2.imread(img_path)
            if img is not None:
                h, w = img.shape[:2]
                mean = float(np.mean(img))
                std = float(np.std(img))
                degenerate = w < 100
                near_black = mean < 15 and std < 3
                if not degenerate and not near_black:
                    return self._load_full(img_path)
                logger.warning(
                    "img bad idx=%d w=%d mean=%.1f std=%.1f degenerate=%s near_black=%s -> planet fallback %s",
                    scene_idx, w, mean, std, degenerate, near_black, img_path,
                )
        else:
            logger.warning("img missing idx=%d -> planet fallback %s", scene_idx, img_path)
        planet = self._planet_path()
        if os.path.exists(planet):
            return self._load_full(planet)
        logger.error("planet cover missing %s, use black", planet)
        return np.zeros((H, W, 3), dtype=np.uint8)

    @staticmethod
    def _load_full(path):
        img = cv2.imread(path)
        if img is None:
            return np.zeros((H, W, 3), dtype=np.uint8)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        scale = max(W / w, H / h)
        nw, nh = int(w * scale), int(h * scale)
        img = cv2.resize(img, (nw, nh))
        x = (nw - W) // 2
        y = (nh - H) // 2
        return img[y : y + H, x : x + W]

    @staticmethod
    def _ken_burns(img, t, total, zoom_max=1.12):
        h, w = img.shape[:2]
        progress = t / max(total, 1)
        bell = (1 - math.cos(progress * 2 * math.pi)) / 2
        zoom = 1.0 + (zoom_max - 1.0) * bell
        nw, nh = int(w * zoom), int(h * zoom)
        resized = cv2.resize(img, (nw, nh))
        pos = (1 - math.cos(progress * 2 * math.pi)) / 2
        x_off = int((nw - W) * pos * 0.4)
        y_off = (nh - H) // 2
        x = min(max(0, x_off), max(0, nw - W))
        y = min(max(0, y_off), max(0, nh - H))
        return resized[y : y + H, x : x + W]

    @staticmethod
    def _put_text(bg, text, x, y, fs=36, color=(255, 255, 230), center=True):
        try:
            font = ImageFont.truetype(FONT_PATH, fs)
        except Exception:
            font = ImageFont.load_default()
        pil = Image.fromarray(cv2.cvtColor(bg, cv2.COLOR_RGB2BGR))
        draw = ImageDraw.Draw(pil)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        tx = x - tw // 2 if center else x
        draw.text((tx, y), text, font=font, fill=color)
        return cv2.cvtColor(np.asarray(pil), cv2.COLOR_BGR2RGB)

    @staticmethod
    def _put_text_box(bg, text, x, y, w, h, fs=32, color=(255, 255, 230), bgc=(0, 0, 0, 160)):
        overlay = bg.copy()
        cv2.rectangle(overlay, (x, y), (x + w, y + h), bgc[:3], -1)
        cv2.addWeighted(overlay, bgc[3] / 255, bg, 1 - bgc[3] / 255, 0, bg)
        try:
            font = ImageFont.truetype(FONT_PATH, fs)
        except Exception:
            font = ImageFont.load_default()
        pil = Image.fromarray(cv2.cvtColor(bg, cv2.COLOR_RGB2BGR))
        draw = ImageDraw.Draw(pil)
        draw.text((x + 20, y + 20), text, font=font, fill=color)
        return cv2.cvtColor(np.asarray(pil), cv2.COLOR_BGR2RGB)

    @staticmethod
    def _put_subtitle(bg, text, x, y, w, color=(255, 255, 230), bgc=(0, 0, 0, 150)):
        # 底部字幕: 自适应字号 + 自动换行 (长旁白片段多行显示), 半透明黑底增强可读性.
        # x,y,w 为字幕区左上角与宽度; 字号根据文字长度在 28-42 间收缩.
        text = str(text).strip()
        if not text:
            return bg
        fs = 42 if len(text) <= 14 else (34 if len(text) <= 28 else 28)
        try:
            font = ImageFont.truetype(FONT_PATH, fs)
        except Exception:
            font = ImageFont.load_default()
        # 按可容纳字符数断行 (中文约 fs 像素/字).
        chars_per_line = max(w // fs, 6)
        lines = []
        cur = ""
        for ch in text:
            cur += ch
            # L11 修: 旧实现只在标点处断行, 长旁白无标点 (如纯叙述句) -> cur 无限长 -> 单行超宽溢出屏幕.
            # 到达容量上限强制软断行, 标点处也断 (优先在标点换行更美观).
            if len(cur) >= chars_per_line and ch in "。！？；，、,.!?; ":
                lines.append(cur)
                cur = ""
            elif len(cur) >= chars_per_line + 4:
                # 超容量 4 字仍无标点 -> 强制断行防溢出.
                lines.append(cur)
                cur = ""
        if cur:
            lines.append(cur)
        if not lines:
            lines = [text]
        line_h = int(fs * 1.4)
        total_h = line_h * len(lines) + 24
        box_y = y
        overlay = bg.copy()
        cv2.rectangle(overlay, (x, box_y), (x + w, box_y + total_h), bgc[:3], -1)
        cv2.addWeighted(overlay, bgc[3] / 255, bg, 1 - bgc[3] / 255, 0, bg)
        pil = Image.fromarray(cv2.cvtColor(bg, cv2.COLOR_RGB2BGR))
        draw = ImageDraw.Draw(pil)
        for li, line in enumerate(lines):
            draw.text((x + 16, box_y + 12 + li * line_h), line, font=font, fill=color)
        return cv2.cvtColor(np.asarray(pil), cv2.COLOR_BGR2RGB)
