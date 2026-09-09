import contextlib
import fcntl
import json
import logging
import os

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def file_lock(path):
    # 跨进程文件锁 (fcntl.flock LOCK_EX): 防 topic_gen 并发读 perf_analyze 半写状态 (审计 P2-10).
    # 锁旁路文件 (path + ".lock"), 持锁期间做 RMW 或原子写, 退出自动释放. 进程崩溃内核自动释放.
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lock_path = str(path) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def parse_script(raw):
    """把 script_json(字符串或 dict) 解析为 dict；非脚本结构返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw if "scenes" in raw or "narration" in raw else None
    if isinstance(raw, str):
        d = None
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            d = None
        # AgentRuntime 扁平插值把 dict 变量按 Python repr 注入 (单引号 {'k':'v'}),
        # 非合法 JSON -> json.loads 失败 -> ast.literal_eval 回退解析 repr.
        if d is None:
            try:
                import ast

                v = ast.literal_eval(raw)
                if isinstance(v, dict):
                    d = v
                elif isinstance(v, str) and v.strip().startswith("{"):
                    d = json.loads(v)
            except (ValueError, SyntaxError, TypeError):
                d = None
        if isinstance(d, dict) and ("scenes" in d or "narration" in d):
            return d
    return None


def extract_prompts(script):
    """从 script dict 取 scenes[].img_prompt 列表；无则空列表。"""
    if not script:
        return []
    scenes = script.get("scenes", [])
    out = []
    for s in scenes:
        if isinstance(s, dict) and s.get("img_prompt"):
            out.append(s["img_prompt"])
    return out


def extract_scenes(script):
    if not script:
        return []
    return script.get("scenes", []) or []


def extract_narration(script):
    if not script:
        return ""
    return script.get("narration", "") or ""


def extract_title(script):
    if not script:
        return ""
    return script.get("title", "") or ""


def extract_description(script):
    if not script:
        return ""
    return script.get("description", "") or ""


def extract_narration_segments(script):
    # 取每个分镜的 narration_segment; 若模型未给或拼接不等于全文, 回退按分镜数等分 narration.
    # 用于 compose 字幕随配音逐段显示 (音画同步).
    if not script:
        return []
    scenes = script.get("scenes", []) or []
    narration = script.get("narration", "") or ""
    segs = []
    for s in scenes:
        if isinstance(s, dict):
            seg = (s.get("narration_segment") or "").strip()
            if seg:
                segs.append(seg)
    # 健壮性: 模型给的片段拼接必须逐字等于全文, 否则丢弃走回退切分.
    if segs and "".join(segs).strip() == narration.strip():
        return segs
    logger.info(
        "narration_segments fallback: model_segs=%d narration_len=%d match=%s",
        len(segs), len(narration),
        "".join(segs).strip() == narration.strip() if segs else False,
    )
    return _split_narration(narration, len(scenes))


def _split_narration(narration, n):
    # 无 narration_segment 时的回退: 按分镜数等分全文旁白 (按字符数近似平分, 尽量在标点断句).
    if n <= 0 or not narration:
        return []
    if n == 1:
        return [narration]
    seps = "。！？；，、,.!?;"
    total = len(narration)
    per = max(total // n, 1)
    segs = []
    start = 0
    for i in range(n):
        if i == n - 1:
            segs.append(narration[start:])
            break
        end = min(start + per, total)
        # 在 [end-5, end+5] 窗口找最近的标点断句, 找不到就硬切.
        best = -1
        for j in range(max(start, end - 5), min(total, end + 5)):
            if narration[j] in seps:
                best = j + 1
                break
        if best > start:
            end = best
        segs.append(narration[start:end])
        start = end
    return segs
