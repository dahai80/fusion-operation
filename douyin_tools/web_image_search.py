import json
import logging
import os
import re
import time
import uuid

import httpx

from . import config
from .config import (
    CELESTIAL_TERMS,
    IMAGE_DIR,
    ensure_dirs,
)
from .script_helpers import extract_prompts, parse_script
from tools.base import BaseTool

logger = logging.getLogger(__name__)


class WebImageSearchTool(BaseTool):
    # 搜 NASA Image Library 真实天体图并下载, 返回 image_paths JSON.
    # 契约同 comfyui_image/mlx_image, Graph C 真实图优先路径.
    # NASA 图公共领域商用合规. 搜不到/不足返回 enough=false 触发上层回退生成图.

    name = "web_image_search"
    description = "搜 NASA Image Library 真实天体图并下载，返回 image_paths JSON（comfyui_image 的真实图优先路径，搜不足 enough=false 触发回退）"
    parameters = {
        "queries": {"type": "string", "description": "天体英文关键词列表(JSON 数组/逗号分隔/或 script_json 自动提取)"},
        "per_query": {"type": "integer", "description": "每个关键词取几张，默认 1"},
        "out_prefix": {"type": "string", "description": "落盘文件名前缀，默认 web_img"},
        "expected": {"type": "integer", "description": "期望产出数(分镜数), 不足则 enough=false 触发回退, 默认 0 不判"},
    }

    async def execute(self, queries, per_query=1, out_prefix="web_img", expected=0, **_):
        ensure_dirs()
        qlist = self._parse_queries(queries)
        if not qlist:
            logger.warning("web_image_search empty queries")
            return json.dumps({"image_paths": [], "enough": False}, ensure_ascii=False)
        pq = max(int(per_query or config.WEB_PER_QUERY), 1)
        image_paths = []
        for qi, query in enumerate(qlist):
            for ni in range(pq):
                dst = os.path.join(IMAGE_DIR, f"{out_prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}_{len(image_paths):03d}.png")
                real = await self._fetch_one(query, ni, dst)
                if real:
                    image_paths.append(real)
                else:
                    logger.warning("web_image_search fail query=%s ni=%d", query, ni)
        # enough: 每个关键词都搜到至少 1 张 (got >= query 数). 不足触发 Graph C 回退全量生成图.
        # expected 仅作外部覆盖下限, 默认 0 不干预.
        # enough 输出字符串 "true"/"false" 适配 ConditionEngine 字符串比较 (同 publish_scheduler).
        need = max(len(qlist), int(expected or 0))
        enough = len(image_paths) >= need
        logger.info("web_image_search done queries=%d got=%d need=%d enough=%s",
                    len(qlist), len(image_paths), need, enough)
        return json.dumps({"image_paths": image_paths, "enough": "true" if enough else "false"}, ensure_ascii=False)

    def _parse_queries(self, val):
        # script_json(dict repr) -> extract_prompts 取 img_prompt -> 抽天体词.
        # 否则按列表/逗号串解析.
        script = parse_script(val)
        if script is not None:
            prompts = extract_prompts(script)
            qlist = [self._extract_celestial(p) for p in prompts]
            logger.info("web_image_search extract from script_json count=%d", len(qlist))
            return [q for q in qlist if q]
        return self._parse_list(val)

    @staticmethod
    def _extract_celestial(prompt):
        # 从 img_prompt 抽首个天体名词作 NASA query (简短名词召回准).
        # 命中不到 -> fallback 前 3 词短语.
        low = prompt.lower()
        for term in CELESTIAL_TERMS:
            if re.search(r"\b" + re.escape(term) + r"\b", low):
                return term
        words = re.findall(r"[a-z]+", low)
        return " ".join(words[:3]) if words else ""

    @staticmethod
    def _parse_list(val):
        if isinstance(val, list):
            return [str(x) for x in val]
        if not val:
            return []
        # 修 R5: dict(JSON/repr) 无 list 结构时显式失败返回 [], 不再 fall through 到 split(",").
        # script_json dict 会先被上层 _parse_queries 的 parse_script 处理, 这里只兜底裸 dict.
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            if isinstance(parsed, dict):
                logger.warning("web_image_search _parse_list dict unexpected: %s", str(val)[:80])
                return []
        except (ValueError, TypeError):
            pass
        try:
            import ast

            parsed = ast.literal_eval(str(val))
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            if isinstance(parsed, dict):
                logger.warning("web_image_search _parse_list ast dict unexpected: %s", str(val)[:80])
                return []
        except (ValueError, SyntaxError):
            pass
        return [p.strip() for p in str(val).split(",") if p.strip()]

    async def _fetch_one(self, query, ni, dst):
        # 单 query 单图: NASA search -> metadata JSON -> 选 large jpg -> 下载.
        # 成功返回真实落盘路径(扩展名按实际), 失败返回 "".
        items = await self._search_nasa(query)
        start = ni
        for item in items[start:]:
            href = item.get("href", "")
            if not href:
                continue
            if self._has_copyright(item):
                logger.info("web_image_search skip copyright nasa_id=%s", item.get("nasa_id", "?"))
                continue
            url = await self._resolve_image_url(href)
            if not url:
                continue
            real = await self._download(url, dst)
            if real:
                self._save_meta(item, real)
                return real
        return ""

    async def _search_nasa(self, query):
        # NASA search 端点, 返回 items 列表 (含 href/data).
        url = f"{config.NASA_API_BASE}/search"
        try:
            async with httpx.AsyncClient(timeout=config.NASA_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url, params={"q": query, "media_type": "image", "page_size": 20})
            if resp.status_code != 200:
                logger.warning("web_image_search nasa http %s query=%s", resp.status_code, query)
                return []
            data = resp.json()
        except Exception as e:
            logger.warning("web_image_search nasa search failed query=%s: %s", query, e)
            return []
        items = data.get("collection", {}).get("items", []) or []
        logger.info("web_image_search nasa ok query=%s items=%d", query, len(items))
        return items

    async def _resolve_image_url(self, item_href):
        # GET metadata JSON -> 选 ~large.jpg (质量足够竖屏裁切, 非 orig 省磁盘).
        try:
            async with httpx.AsyncClient(timeout=config.NASA_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(item_href)
            if resp.status_code != 200:
                logger.warning("web_image_search meta http %s href=%s", resp.status_code, item_href)
                return ""
            files = resp.json()
        except Exception as e:
            logger.warning("web_image_search meta failed href=%s: %s", item_href, e)
            return ""
        if not isinstance(files, list):
            return ""
        urls = [f for f in files if isinstance(f, str) and f.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff"))]
        for u in urls:
            if "~large" in u:
                return u
        for u in urls:
            if "~medium" in u:
                return u
        return urls[0] if urls else ""

    async def _download(self, url, dst):
        # 成功返回真实落盘路径(扩展名按实际 url), 失败返回 "".
        try:
            async with httpx.AsyncClient(timeout=config.NASA_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("web_image_search dl http %s url=%s", resp.status_code, url[:80])
                return ""
            content = resp.content
            if not content or len(content) < 1000:
                logger.warning("web_image_search dl too small url=%s", url[:80])
                return ""
            # NASA 多为 jpg, 按实际扩展名落盘 (.tif 转 .jpg 无意义, 保留原格式更诚实).
            ext = ".jpg"
            low = url.lower()
            for e in (".png", ".tif", ".tiff"):
                if low.endswith(e):
                    ext = ".jpg" if e in (".tif", ".tiff") else e
                    break
            real_dst = dst[:-4] + ext if dst.endswith(".png") else dst
            with open(real_dst, "wb") as f:
                f.write(content)
            logger.info("web_image_search dl ok url=%s -> %s (%d bytes)", url[:80], real_dst, len(content))
            return real_dst
        except Exception as e:
            logger.warning("web_image_search dl failed url=%s: %s", url[:80], e)
            return ""

    @staticmethod
    def _has_copyright(item):
        # NASA 图公共领域, 但库含少量合作机构带版权图 -> 跳过.
        data_list = item.get("data", [])
        if not isinstance(data_list, list) or not data_list:
            return False
        d = data_list[0] if isinstance(data_list[0], dict) else {}
        cp = str(d.get("copyright", "") or "").strip()
        desc = str(d.get("description", "") or "").lower()
        if cp:
            return True
        if "copyright" in desc and "public domain" not in desc:
            return True
        return False

    @staticmethod
    def _save_meta(item, dst):
        # 落盘 metadata 留痕 (nasa_id/title/photographer), 备查来源.
        data_list = item.get("data", [])
        d = data_list[0] if isinstance(data_list, list) and data_list and isinstance(data_list[0], dict) else {}
        meta = {
            "nasa_id": d.get("nasa_id", ""),
            "title": d.get("title", ""),
            "photographer": d.get("photographer", ""),
            "description": str(d.get("description", ""))[:200],
        }
        base = dst.rsplit(".", 1)[0]
        meta_path = base + "_meta.json"
        try:
            tmp = meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(tmp, meta_path)
        except OSError:
            pass
