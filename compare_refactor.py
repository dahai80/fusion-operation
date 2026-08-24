import asyncio
import json
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s INFO %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("compare")

from douyin_tools.mlx_script import MlxScriptTool
from douyin_tools.mlx_image import MlxImageTool
from douyin_tools.mlx_tts import MlxTtsTool
from douyin_tools.comfyui_produce import ComfyUIImageTool
from douyin_tools.comfyui_tts import ComfyUITtsTool
from douyin_tools.ffmpeg_compose import FfmpegComposeTool
from douyin_tools.config import ensure_dirs

OUT = os.path.join(os.path.dirname(__file__), "out", "compare")
os.makedirs(OUT, exist_ok=True)


async def gen_once(label, image_tool, tts_tool, script_json, topic):
    sub = os.path.join(OUT, label)
    os.makedirs(sub, exist_ok=True)
    logger.info("=== %s start ===", label)

    img_res = await image_tool.execute(prompts=script_json, n=1, size="1024x1024")
    img_data = json.loads(img_res)
    if "error" in img_data:
        logger.error("%s image failed: %s", label, img_data["error"])
        return None
    image_paths = img_data["image_paths"]
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    logger.info("%s image done paths=%d", label, len(image_paths))

    tts_res = await tts_tool.execute(text=script_json, out_name=f"{label}_{topic}")
    tts_data = json.loads(tts_res)
    if "error" in tts_data:
        logger.error("%s tts failed: %s", label, tts_data["error"])
        return None
    audio_path = tts_data["audio_path"]
    logger.info("%s tts done audio=%s", label, os.path.basename(audio_path))

    compose_res = await FfmpegComposeTool().execute(
        image_paths=image_paths,
        audio_path=audio_path,
        scenes=script_json,
        out_name=f"{label}_{topic}",
    )
    comp = json.loads(compose_res)
    if "error" in comp:
        logger.error("%s compose failed: %s", label, comp["error"])
        return None
    video_path = comp["video_path"]
    logger.info("%s compose done video=%s", label, video_path)
    return video_path


async def main():
    ensure_dirs()

    topic = "如果把太阳压缩成一颗豌豆，你会瞬间爆炸吗"
    logger.info("generate script topic=%s", topic)
    script_res = await MlxScriptTool().execute(
        topic=topic, series="宇宙奥秘", hook_variant="A"
    )
    script_data = json.loads(script_res)
    if "error" in script_data or "scenes" not in script_data:
        logger.error("script gen failed: %s", script_res[:200])
        return
    script_json = script_data
    script_path = os.path.join(OUT, "script.json")
    with open(script_path, "w") as f:
        json.dump(script_json, f, ensure_ascii=False, indent=2)
    logger.info("script saved title=%s -> %s", script_data.get("title"), script_path)

    before = await gen_once("before", MlxImageTool(), MlxTtsTool(), script_json, "sun_pea")
    after = await gen_once("after", ComfyUIImageTool(), ComfyUITtsTool(), script_json, "sun_pea")

    logger.info("===== compare result =====")
    logger.info("before (mlx_image+mlx_tts+ffmpeg): %s", before or "FAIL")
    logger.info("after  (comfyui_image+comfyui_tts+ffmpeg): %s", after or "FAIL")
    if before and after:
        sb = os.path.getsize(before) / 1024 / 1024
        sa = os.path.getsize(after) / 1024 / 1024
        logger.info("before size=%.2fMB  after size=%.2fMB", sb, sa)
    logger.info("compare dir: %s", OUT)


if __name__ == "__main__":
    asyncio.run(main())
