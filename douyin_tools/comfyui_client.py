import asyncio
import glob
import logging
import os
import time

import httpx

from . import config
from .config import COMFYUI_AUDIO_TEMP_DIR, COMFYUI_OUTPUT_DIR

logger = logging.getLogger(__name__)


class ComfyUIClient:
    # 标准 ComfyUI HTTP client: /prompt 提交 + /history 轮询 + 输出路径解析.
    # 调用方: comfyui_produce 工具 (造片链), 业务层封装, 不依赖上游改 client helper.
    # base/timeout 审计 H5: 用动态常量 (config.X 属性访问), 改 JSON 立即生效. 默认参数在
    # def 时求值只绑一次, 故默认 None 在 __init__ 内实时读 config (修 H5 快照陷阱).

    def __init__(self, base=None, timeout=None):
        self.base = (base if base is not None else config.COMFYUI_BASE).rstrip("/")
        self.timeout = timeout if timeout is not None else config.COMFYUI_TIMEOUT

    async def health(self):
        # /system_stats 探活, 返回 bool.
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.base}/system_stats")
            return r.status_code == 200
        except Exception as e:
            logger.warning("comfyui health failed: %s", e)
            return False

    async def submit(self, graph, client_id="fusion-operation"):
        # 提交 prompt graph, 返回 prompt_id. graph 为扁平 dict: {node_id: {class_type, inputs}}.
        payload = {"prompt": graph, "client_id": client_id}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{self.base}/prompt", json=payload)
            if r.status_code != 200:
                logger.error("comfyui /prompt http %s: %s", r.status_code, r.text[:300])
                return ""
            data = r.json()
            pid = data.get("prompt_id", "")
            if not pid:
                logger.error("comfyui /prompt no prompt_id: %s", str(data)[:300])
            logger.info("comfyui submit ok prompt_id=%s nodes=%d", pid, len(graph))
            return pid
        except Exception as e:
            logger.error("comfyui submit failed: %s", e)
            return ""

    async def wait_result(self, prompt_id, poll_interval=2.0, max_wait=None):
        # 轮询 /history 直到 prompt_id 完成, 返回 {status, outputs} 或 None.
        # status: success / 失败 (history 里 status.status_str). outputs: {node_id: {videos/images:[...]}}.
        # 复用单个 AsyncClient 跑完整个轮询, 不每次 new client (修 P1: 每 2s new client 浪费连接).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait if max_wait else None
        waited = 0
        async with httpx.AsyncClient(timeout=30) as c:
            while True:
                if deadline and loop.time() > deadline:
                    logger.warning("comfyui wait timeout prompt_id=%s waited=%.0fs", prompt_id, waited)
                    return None
                try:
                    r = await c.get(f"{self.base}/history?max_items=50")
                    if r.status_code == 200:
                        hist = r.json()
                        item = hist.get(prompt_id)
                        if item:
                            status = item.get("status", {})
                            status_str = status.get("status_str", "")
                            outputs = item.get("outputs", {})
                            logger.info("comfyui done prompt_id=%s status=%s outputs=%d", prompt_id, status_str, len(outputs))
                            return {"status": status_str, "outputs": outputs}
                except Exception as e:
                    logger.warning("comfyui poll error: %s", e)
                await asyncio.sleep(poll_interval)
                waited += poll_interval

    @staticmethod
    def resolve_output(output_entry, output_dir=COMFYUI_OUTPUT_DIR):
        # 解析 history outputs 单节点 → 本地绝对路径列表.
        # output_entry 形如 {"videos":[{"filename":"x.mp4","subfolder":""}]} 或 {"images":[...]}.
        paths = []
        for kind in ("videos", "images", "gifs", "audio"):
            items = output_entry.get(kind)
            if not items:
                continue
            for it in items:
                fn = it.get("filename", "")
                sub = it.get("subfolder", "")
                if not fn:
                    continue
                p = os.path.join(output_dir, sub, fn) if sub else os.path.join(output_dir, fn)
                if os.path.exists(p):
                    paths.append(p)
                else:
                    logger.warning("comfyui output missing on disk: %s", p)
        return paths

    async def run_graph(self, graph, max_wait=None):
        # 端到端: 提交 + 等待 + 解析, 返回 {prompt_id, status, paths}.
        # paths 来自 /history outputs 的 videos/images/gifs/audio kind (标准 ComfyUI output).
        # 注意: Fusion 音频节点存到 tempdir 不进 /history, 用 resolve_audio_by_prefix 单独回找.
        pid = await self.submit(graph)
        if not pid:
            return {"prompt_id": "", "status": "submit_failed", "paths": []}
        res = await self.wait_result(pid, max_wait=max_wait)
        if not res:
            return {"prompt_id": pid, "status": "timeout", "paths": []}
        all_paths = []
        for nid, entry in res["outputs"].items():
            all_paths.extend(self.resolve_output(entry))
        return {"prompt_id": pid, "status": res["status"], "paths": all_paths}

    @staticmethod
    def resolve_audio_by_prefix(prefix, audio_dir=COMFYUI_AUDIO_TEMP_DIR):
        # Fusion 节点把 wav 存到 tempdir/fusion_comfyui_audio/{prefix}_NNNN.wav, 不进 /history.
        # 业务层传唯一 prefix, 执行后按 glob 回找最新匹配文件. 返回绝对路径或 "".
        if not prefix:
            return ""
        pattern = os.path.join(audio_dir, f"{prefix}_*.wav")
        matches = glob.glob(pattern)
        if not matches:
            logger.warning("comfyui audio prefix no match: %s", pattern)
            return ""
        matches.sort(key=os.path.getmtime)
        path = matches[-1]
        logger.info("comfyui audio resolved prefix=%s -> %s", prefix, path)
        ComfyUIClient._clean_stale_audio(audio_dir)
        return path

    @staticmethod
    def _clean_stale_audio(audio_dir, max_age_days=1):
        # 清理超过 max_age_days 的临时 wav, 防 /tmp/fusion_comfyui_audio 无界膨胀 (修 P2).
        # 当前业务的 wav 已由 comfyui_tts 复制到 AUDIO_DIR, temp 目录只剩历史残留.
        try:
            now = time.time()
            cutoff = now - max_age_days * 86400
            removed = 0
            for fn in os.listdir(audio_dir):
                if not fn.endswith(".wav"):
                    continue
                p = os.path.join(audio_dir, fn)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
            if removed:
                logger.info("comfyui cleaned stale temp audio removed=%d dir=%s", removed, audio_dir)
        except OSError:
            pass
