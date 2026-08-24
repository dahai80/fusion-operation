import json
import logging
import os
import shutil
import time
import uuid

from tools.base import BaseTool

from . import config
from .comfyui_client import ComfyUIClient
from .config import (
    AUDIO_DIR,
    ensure_dirs,
)

logger = logging.getLogger(__name__)


class ComfyUITtsTool(BaseTool):
    # ComfyUI TTS: 替代 mlx_tts 的优先路径, 失败由上层回退到 mlx_tts.
    # graph: FusionVoiceLoader -> FusionVoiceSynthesize(text,voice,speed) -> FusionSaveAudio(prefix).
    # Fusion 音频节点存到 tempdir 不进 /history, 用唯一 prefix + resolve_audio_by_prefix 回找,
    # 再复制到 fusion-operation AUDIO_DIR 统一管理. 契约同 mlx_tts: 返回 {"audio_path": "..."}.

    name = "comfyui_tts"
    description = "ComfyUI 文本转语音：FusionVoiceSynthesize 合成 + FusionSaveAudio 落盘，返回音频路径 JSON（mlx_tts 的 ComfyUI 优先路径）"
    parameters = {
        "text": {"type": "string", "description": "要合成的文本(或脚本 JSON, 自动抽取旁白)"},
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
            logger.warning("comfyui_tts empty text")
            return json.dumps({"error": "empty text"})
        # http backend 用 Qwen3-TTS Cherry 中文音色; local 走 Kokoro af_heart.
        # 审计 H5: backend/voice 是动态常量, config.X 实时读 (改 JSON 立即生效).
        if not voice or not str(voice).strip():
            voice = config.COMFYUI_TTS_HTTP_VOICE if config.COMFYUI_TTS_BACKEND == "http" else config.DEFAULT_TTS_VOICE
        ensure_dirs()
        client = ComfyUIClient()
        if not await client.health():
            logger.warning("comfyui unavailable, caller should fallback to mlx_tts")
            return json.dumps({"error": "comfyui unavailable"})
        name = out_name or f"tts_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        prefix = f"fo_{name}"
        graph = self._build_graph(text, voice, speed, prefix)
        res = await client.run_graph(graph, max_wait=300)
        if res["status"] != "success":
            logger.warning("comfyui_tts failed status=%s", res["status"])
            return json.dumps({"error": f"comfyui tts failed: {res['status']}"})
        # 上游 PR#19 已让 FusionSaveAudio 进 /history outputs, run_graph 的 paths 直接含音频.
        # 优先用标准 /history 路径, 回退到 prefix 回找 tempdir (旧节点兼容).
        src = ""
        for p in res.get("paths", []):
            if p.endswith(".wav"):
                src = p
                break
        if not src or not os.path.exists(src):
            logger.info("comfyui_tts /history no audio, fallback prefix=%s", prefix)
            src = client.resolve_audio_by_prefix(prefix)
        if not src or not os.path.exists(src):
            logger.warning("comfyui_tts audio not found prefix=%s", prefix)
            return json.dumps({"error": "comfyui tts audio missing"})
        dst = os.path.join(AUDIO_DIR, f"{name}.wav")
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            logger.warning("comfyui_tts copy failed %s: %s", src, e)
            return json.dumps({"error": f"copy failed: {e}"})
        logger.info("comfyui_tts done path=%s", dst)
        return json.dumps({"audio_path": dst}, ensure_ascii=False)

    def _build_graph(self, text, voice, speed, prefix):
        synth_inputs = {
            "tts_engine": ["1", 0],
            "text": text,
            "voice": voice,
            "speed": speed,
        }
        # http backend: 走 fusion-mlx /v1/audio/speech, model_name 指定 Qwen3-TTS.
        # local backend (默认兼容): 走 Kokoro in-process.
        # 审计 H5: backend/model 是动态常量, config.X 实时读.
        if config.COMFYUI_TTS_BACKEND == "http":
            synth_inputs["backend"] = "http"
            synth_inputs["model_name"] = config.COMFYUI_TTS_HTTP_MODEL
        graph = {
            "1": {
                "class_type": "FusionVoiceLoader",
                "inputs": {"model_name": config.COMFYUI_TTS_MODEL},
            },
            "2": {
                "class_type": "FusionVoiceSynthesize",
                "inputs": synth_inputs,
            },
            "3": {
                "class_type": "FusionSaveAudio",
                "inputs": {
                    "audio": ["2", 0],
                    "filename_prefix": prefix,
                    "sample_rate": 24000,
                },
            },
        }
        logger.info(
            "comfyui_tts graph built backend=%s model=%s text_len=%d",
            config.COMFYUI_TTS_BACKEND, config.COMFYUI_TTS_MODEL, len(text),
        )
        return graph
