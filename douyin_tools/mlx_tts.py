import json
import logging
import os
import time
import uuid

from . import config
from .config import (
    AUDIO_DIR,
    ensure_dirs,
    mlx_headers,
)
from .resilient_http import post_json
from tools.base import BaseTool

logger = logging.getLogger(__name__)


class MlxTtsTool(BaseTool):
    name = "mlx_tts"
    description = "文本转语音：优先 fusion-mlx /v1/audio/speech，失败回退 edge-tts，返回音频路径 JSON"
    roles = frozenset({"voicer"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "text": {"type": "string", "description": "要合成的文本"},
        "voice": {"type": "string", "description": "音色，默认 af_heart"},
        "speed": {"type": "number", "description": "语速，默认 1.0"},
        "out_name": {"type": "string", "description": "输出文件名(不含扩展)，默认自动"},
    }

    async def execute(self, text, voice="", speed=1.0, out_name="", **_):
        from .script_helpers import parse_script, extract_narration
        script = parse_script(text)
        if script:
            text = extract_narration(script) or ""
        if not text or not str(text).strip():
            logger.warning("mlx_tts empty text")
            return json.dumps({"error": "empty text"})
        voice = voice or config.DEFAULT_TTS_VOICE
        ensure_dirs()
        name = out_name or f"tts_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        audio_path = await self._gen_mlx(text, voice, speed, name)
        if not audio_path:
            audio_path = await self._gen_edge(text, voice, name)
        if not audio_path:
            logger.error("mlx_tts all backends failed")
            return json.dumps({"error": "tts failed"})
        logger.info("mlx_tts done path=%s", audio_path)
        return json.dumps({"audio_path": audio_path}, ensure_ascii=False)

    async def _gen_mlx(self, text, voice, speed, name):
        url = f"{config.MLX_BASE}/v1/audio/speech"
        payload = {
            "model": config.DEFAULT_TTS_MODEL,
            "input": text,
            "voice": voice,
            "speed": speed,
            "response_format": "wav",
        }
        try:
            resp = await post_json(url, payload, service="mlx")
            if resp.status_code != 200:
                logger.warning("mlx tts http %s: %s", resp.status_code, resp.text[:200])
                return ""
            out = os.path.join(AUDIO_DIR, f"{name}.wav")
            with open(out, "wb") as f:
                f.write(resp.content)
            logger.info("mlx tts ok via fusion-mlx %s", out)
            return out
        except Exception as e:
            logger.warning("mlx tts failed, try edge-tts: %s", e)
            return ""

    async def _gen_edge(self, text, voice, name):
        try:
            import asyncio
            import subprocess

            import edge_tts

            edge_voice = "zh-CN-YunyangNeural"
            mp3 = os.path.join(AUDIO_DIR, f"{name}.mp3")

            def _run():
                asyncio.run(edge_tts.Communicate(text, edge_voice).save(mp3))

            await asyncio.to_thread(_run)
            wav = os.path.join(AUDIO_DIR, f"{name}.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp3, "-ar", "24000", "-ac", "1", wav],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if os.path.exists(wav):
                logger.info("edge-tts ok %s", wav)
                return wav
            logger.warning("edge-tts wav missing, fallback mp3")
            return mp3
        except Exception as e:
            logger.error("edge-tts failed: %s", e)
            return ""
