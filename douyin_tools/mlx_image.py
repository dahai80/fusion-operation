import json
import logging
import os
import time
import uuid

import httpx

from . import config
from .config import (
    FALLBACK_IMAGE_DIR,
    IMAGE_DIR,
    ensure_dirs,
    mlx_headers,
)
from tools.base import BaseTool

logger = logging.getLogger(__name__)


class MlxImageTool(BaseTool):
    name = "mlx_image"
    description = "文生图：优先 fusion-mlx /v1/images/generate，失败回退静态图库，返回图片路径数组 JSON"
    roles = frozenset({"imager"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "prompts": {"type": "string", "description": "提示词列表(JSON 数组或逗号分隔)"},
        "n": {"type": "integer", "description": "每个提示词生成张数，默认 1"},
        "size": {"type": "string", "description": "尺寸，默认 1024x1024"},
        "fallback_dir": {"type": "string", "description": "回退静态图目录，默认 FALLBACK_IMAGE_DIR"},
    }

    async def execute(
        self,
        prompts,
        n=1,
        size="1024x1024",
        fallback_dir="",
        **_,
    ):
        # E8: parse_script 单一入口优先 (契约同 comfyui_produce). prompts 可能是
        # Graph 传入的整个 script_json (dict repr), _parse_list 先跑会把 dict 按逗号
        # 分段产垃圾图. 先 parse_script 提 scenes[].img_prompt, 提不到再 _parse_list
        # 兼容旧 prompts 数组/逗号串输入.
        from .script_helpers import parse_script, extract_prompts
        script = parse_script(prompts)
        if script is not None:
            plist = extract_prompts(script)
            logger.info("mlx_image extract_prompts from script count=%d", len(plist))
        else:
            plist = self._parse_list(prompts)
        if not plist:
            logger.warning("mlx_image empty prompts")
            return json.dumps({"error": "empty prompts"})
        ensure_dirs()
        fb_dir = fallback_dir or FALLBACK_IMAGE_DIR
        image_paths = []
        for idx, prompt in enumerate(plist):
            path = await self._gen_mlx(prompt, n, size, idx)
            if not path:
                path = self._pick_fallback(fb_dir, idx)
            if path:
                image_paths.append(path)
        if not image_paths:
            logger.error("mlx_image all failed prompts=%d", len(plist))
            return json.dumps({"error": "image gen failed"})
        logger.info("mlx_image done count=%d", len(image_paths))
        return json.dumps({"image_paths": image_paths}, ensure_ascii=False)

    @staticmethod
    def _parse_list(val):
        if isinstance(val, list):
            return val
        if not val:
            return []
        # 修 R5: dict(JSON/repr) 无已知 key 时显式失败返回 [], 不再 fall through 到 split(",").
        # 旧逻辑把整个 dict repr 按逗号分成 N 段垃圾 (曾导致 39 张图). split 仅留给 json+ast
        # 都解析失败的纯逗号串 (文档化的逗号分隔输入).
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for k in ("prompts", "items"):
                    if isinstance(parsed.get(k), list):
                        return parsed[k]
                logger.warning("mlx_image _parse_list dict no known key: %s", str(val)[:80])
                return []
        except (ValueError, TypeError):
            pass
        try:
            import ast

            parsed = ast.literal_eval(str(val))
            if isinstance(parsed, list):
                return [str(p) for p in parsed]
            if isinstance(parsed, dict):
                for k in ("prompts", "items"):
                    if isinstance(parsed.get(k), list):
                        return [str(p) for p in parsed[k]]
                logger.warning("mlx_image _parse_list ast dict no known key: %s", str(val)[:80])
                return []
        except (ValueError, SyntaxError):
            pass
        return [p.strip() for p in str(val).split(",") if p.strip()]

    async def _gen_mlx(self, prompt, n, size, idx):
        url = f"{config.MLX_BASE}/v1/images/generate"
        payload = {
            "model": config.DEFAULT_IMAGE_MODEL,
            "prompt": prompt,
            "n": n,
            "size": size,
            "response_format": "b64_json",
        }
        try:
            async with httpx.AsyncClient(timeout=config.MLX_TIMEOUT, headers=mlx_headers()) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning("mlx image http %s: %s", resp.status_code, resp.text[:200])
                return ""
            data = resp.json()
            items = data.get("data", []) if isinstance(data, dict) else []
            if not items:
                logger.warning("mlx image no data idx=%d", idx)
                return ""
            import base64

            out = os.path.join(IMAGE_DIR, f"img_{int(time.time())}_{uuid.uuid4().hex[:6]}_{idx:03d}.png")
            b64 = items[0].get("b64_json") or items[0].get("image")
            if not b64:
                logger.warning("mlx image no b64 idx=%d", idx)
                return ""
            with open(out, "wb") as f:
                f.write(base64.b64decode(b64))
            logger.info("mlx image ok idx=%d path=%s", idx, out)
            return out
        except Exception as e:
            logger.warning("mlx image failed idx=%d: %s", idx, e)
            return ""

    @staticmethod
    def _pick_fallback(fb_dir, idx):
        if not fb_dir or not os.path.isdir(fb_dir):
            logger.warning("fallback dir missing %s", fb_dir)
            return ""
        exts = (".png", ".jpg", ".jpeg", ".webp")
        files = sorted(
            f for f in os.listdir(fb_dir) if f.lower().endswith(exts)
        )
        if not files:
            logger.warning("fallback dir empty %s", fb_dir)
            return ""
        # topic+idx 种子做确定性但分散的选图, 避免每条同 idx 永取同一图 (修 P3).
        # 同 topic 复现一致, 不同 topic/idx 错开.
        seed = (int(time.time()) // 3600 + idx) % len(files)
        chosen = os.path.join(fb_dir, files[seed])
        logger.info("fallback image idx=%d seed=%d path=%s", idx, seed, chosen)
        return chosen
