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
    IMAGE_DIR,
    ensure_dirs,
)
from .script_helpers import extract_prompts, parse_script

logger = logging.getLogger(__name__)


class ComfyUIImageTool(BaseTool):
    # ComfyUI 文生图: 替代 mlx_image 的优先路径, 失败由上层回退到 mlx_image.
    # 单 graph 共享一个 FusionModelLoader, N 个 FusionImageGen + SaveImage 落盘.
    # 契约同 mlx_image: 返回 {"image_paths": [...]}.

    name = "comfyui_image"
    description = "ComfyUI 文生图：FusionImageGen 生图落盘，返回图片路径数组 JSON（mlx_image 的 ComfyUI 优先路径）"
    roles = frozenset({"imager"})  # M3-1: role-based tool filtering (architecture §6.1)
    parameters = {
        "prompts": {"type": "string", "description": "提示词列表(JSON 数组或逗号分隔)"},
        "n": {"type": "integer", "description": "每个提示词生成张数，默认 1"},
        "size": {"type": "string", "description": "尺寸，默认 1080x1920 竖屏(抖音9:16)"},
        "steps": {"type": "integer", "description": "采样步数，默认 8"},
    }

    async def execute(self, prompts, n=1, size="1080x1920", steps=8, **_):
        # prompts 可能是 Graph C 传入的整个 script_json(dict repr).
        # 旧逻辑直接 _parse_list 把 dict repr 按逗号分 39 段 → 39 张图.
        # 先尝试 parse_script 提取 scenes[].img_prompt, 提取到则用之.
        script = parse_script(prompts)
        if script is not None:
            plist = extract_prompts(script)
            logger.info("comfyui_image extract_prompts from script_json count=%d", len(plist))
        else:
            plist = self._parse_list(prompts)
        if not plist:
            logger.warning("comfyui_image empty prompts")
            return json.dumps({"error": "empty prompts"})
        ensure_dirs()
        client = ComfyUIClient()
        if not await client.health():
            logger.warning("comfyui unavailable, caller should fallback to mlx_image")
            return json.dumps({"error": "comfyui unavailable"})
        try:
            w, h = (int(x) for x in size.lower().split("x"))
        except Exception:
            w, h = 1024, 1024
        graph = self._build_graph(plist, n, w, h, steps)
        res = await client.run_graph(graph, max_wait=900)
        if res["status"] != "success" or not res["paths"]:
            logger.warning("comfyui_image failed status=%s paths=%d", res["status"], len(res["paths"]))
            return json.dumps({"error": f"comfyui gen failed: {res['status']}"})
        image_paths = self._collect_to_image_dir(res["paths"], len(plist))
        logger.info("comfyui_image done count=%d", len(image_paths))
        return json.dumps({"image_paths": image_paths}, ensure_ascii=False)

    def _build_graph(self, prompts, n, w, h, steps):
        # FusionModelLoader (节点1) 共享; 每个 prompt 一个 ImageGen + SaveImage.
        # filename_prefix 用 prompt 索引, 便于 _collect 按 prefix 分组取首张.
        graph = {
            "1": {
                "class_type": "FusionModelLoader",
                "inputs": {
                    "model_name": config.COMFYUI_IMAGE_MODEL,
                    "offload_strategy": "sequential",
                    "quant_bit": "fp8_e4m3",
                },
            },
        }
        node_id = 2
        for pi, prompt in enumerate(prompts):
            for ni in range(n):
                gen_id = str(node_id)
                save_id = str(node_id + 1)
                graph[gen_id] = {
                    "class_type": "FusionImageGen",
                    "inputs": {
                        "pipeline": ["1", 0],
                        "prompt": prompt,
                        # 天体科普视频禁人物: 生成层确定性兜底, 即使 img_prompt 漏写排除词也拦下.
                        "negative_prompt": "people, person, human, astronaut, figure, face, portrait, man, woman, child, crowd, silhouette of person",
                        "width": w,
                        "height": h,
                        "steps": steps,
                        "cfg": 6.0,
                        "seed": 42 + pi * 100 + ni,
                    },
                }
                graph[save_id] = {
                    "class_type": "SaveImage",
                    "inputs": {
                        # 修 C3: 加 uuid 运行唯一前缀, 并发 cron 同 topic 同 prompt 也不在 ComfyUI 输出目录撞名.
                        "images": [gen_id, 0],
                        "filename_prefix": f"fo_img_{uuid.uuid4().hex[:6]}_{pi:03d}_{ni:03d}",
                    },
                }
                node_id += 2
        logger.info("comfyui_image graph built prompts=%d n=%d nodes=%d", len(prompts), n, len(graph))
        return graph

    @staticmethod
    def _collect_to_image_dir(src_paths, n_prompts):
        # ComfyUI 落盘在 COMFYUI_OUTPUT_DIR, 复制到 fusion-operation IMAGE_DIR 统一管理.
        # SaveImage 可能对单次输出存多帧 (filename 加 _NNNNN_ 序号), 按 filename_prefix 分组取首张.
        out = []
        seen_prefix = set()
        for src in src_paths:
            if not os.path.exists(src):
                logger.warning("comfyui_image src missing: %s", src)
                continue
            base = os.path.basename(src)
            # fo_img_{uuid6}_{pi:03d}_{ni:03d}_NNNNN_.png → 取 prefix = fo_img_{uuid6}_{pi}_{ni}
            # uuid 前缀保证并发 cron 不撞名, 按 prefix 分组取首张仍正确 (同次运行同 pi_ni 落同组).
            parts = base.split("_")
            if len(parts) >= 6:
                prefix = "_".join(parts[:5])
            else:
                prefix = base.rsplit(".", 1)[0]
            if prefix in seen_prefix:
                continue
            seen_prefix.add(prefix)
            idx = len(out)
            dst = os.path.join(IMAGE_DIR, f"img_{int(time.time())}_{uuid.uuid4().hex[:6]}_{idx:03d}.png")
            try:
                shutil.copy2(src, dst)
                out.append(dst)
            except Exception as e:
                logger.warning("comfyui_image copy failed %s: %s", src, e)
        logger.info("comfyui_image collected=%d expected=%d", len(out), n_prompts)
        return out

    @staticmethod
    def _parse_list(val):
        if isinstance(val, list):
            return val
        if not val:
            return []
        # 修 R5: dict(JSON/repr) 无已知 key 时显式失败返回 [], 不再 fall through 到 split(",").
        # 旧逻辑把整个 script_json dict repr 按逗号分成 39 段画 39 张图 (已修, 此处固化).
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for k in ("prompts", "items"):
                    if isinstance(parsed.get(k), list):
                        return parsed[k]
                logger.warning("comfyui_image _parse_list dict no known key: %s", str(val)[:80])
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
                logger.warning("comfyui_image _parse_list ast dict no known key: %s", str(val)[:80])
                return []
        except (ValueError, SyntaxError):
            pass
        return [p.strip() for p in str(val).split(",") if p.strip()]
