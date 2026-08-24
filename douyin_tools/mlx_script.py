import json
import logging
import os

import httpx

from . import config
from .config import OPS_DATA_DIR, ensure_dirs, get_json_config, mlx_headers
from tools.base import BaseTool

logger = logging.getLogger(__name__)

PATTERNS_FILE = os.path.join(OPS_DATA_DIR, "winning_patterns.json")

# 业务参数: 模块级保留 import 时绑定值 (兼容旧测试 test_declarative_config_override).
# 运行时实时读 (修 A1: 改配置不重启 daemon 也生效).
_SCRIPT_CFG = get_json_config().get("script", {})
MAX_SCENES = int(_SCRIPT_CFG.get("max_scenes", 5))

_DEFAULT_VARIANT_HOOK = {
    "A": "数字+反常：首句用一个极端数字搭配反常识结论",
    "B": "提问+代入：首句用第二人称提问把观众代入场景",
    "C": "悬念+冲突：首句抛出一个待解的悬念冲突",
}
VARIANT_HOOK = _SCRIPT_CFG.get("hook_variants", _DEFAULT_VARIANT_HOOK)


def _script_cfg():
    # 每次调用实时读配置 (修 A1: 模块级绑定在 daemon 长进程不重 import -> 改配置失效).
    return get_json_config().get("script", {})

SYSTEM_PROMPT = """你是抖音科普吸粉脚本作家。按以下规则输出严格 JSON（不要任何额外文字、不要 markdown 代码块）：
{
  "title": "作品标题，应用 悬念/冲突 + 信息量 + 情绪 公式，≤20字",
  "description": "作品描述，含2-3个#话题，≤100字",
  "narration": "全文旁白，口语化，约150-220字，首句必须是3秒强钩子",
  "hook_type": "3秒钩子类型：悬念/反常/提问/数字",
  "scenes": [
    {"img_prompt": "该分镜配图的英文生成提示词", "cn_text": "该分镜画面上显示的中文字幕(≤14字短句)", "scene": "场景标签", "narration_segment": "该分镜对应朗读的旁白片段(把全文旁白按分镜顺序切分, 所有片段拼接起来必须等于全文narration, 逐字不漏不重复)"}
  ],
  "ending_hook": "结尾钩子：选择题或投票引导评论 + 必带关注引导话术"
}
要求：
- 首句3秒钩子必须抓人（悬念/反常/提问/数字 四选一）
- 分镜严格 4-5 个，绝不超过 5 个（短视频 15-30 秒，过多分镜会拖慢生产且画面切换过快）
- 每个分镜 img_prompt 要具体可生成，风格统一（如太空/科幻/写实），且提示词末尾加 "vertical composition, 9:16 portrait, cinematic" 以适配竖屏
- **画面只画天体/宇宙**：所有分镜 img_prompt 必须是星球/恒星/星云/星系/黑洞/宇宙景观等天体，严禁出现任何人物、宇航员、人脸、人形剪影、人眼特写——这是天体科普不是人物故事，"代入感"靠旁白和字幕而非画面里画人。img_prompt 末尾务必加 "no people, no human, no astronaut, no person, no face, no figure" 排除词
- 首个分镜（封面帧/抖音首页静态展示）img_prompt 必须以醒目的星球或天体为主体（地球/木星/恒星等），画面明亮有色彩层次，禁止纯黑太空底——首页缩略图黑底无点击欲。后续分镜可含深空暗调
- scenes 的 narration_segment 是音画同步的关键：所有分镜的 narration_segment 顺序拼接必须逐字等于 narration 全文，用于字幕随配音逐段显示
- cn_text 是屏幕字幕短句(≤14字)，与 narration_segment 内容呼应但更精炼，避免文字过密
- 结尾用选择题或投票引导用户评论，提升互动率
- 结尾必须含关注引导话术，如"关注我，每天一个颠覆常识的宇宙真相"或"点个关注，下期更颠覆"，把评论互动转化为粉丝转化
- description 末尾必带"关注我"引导
- 不出现水印、引流、敏感词
"""


class MlxScriptTool(BaseTool):
    name = "mlx_script"
    description = "用 fusion-mlx 生成抖音吸粉脚本 JSON（标题/旁白/分镜/结尾钩子），支持 hook_variant A/B/C 实验 + 注入 winning_hooks 回灌"
    parameters = {
        "topic": {"type": "string", "description": "本期主题，如 太阳系边缘"},
        "series": {"type": "string", "description": "所属系列，如 宇宙奥秘"},
        "style": {"type": "string", "description": "风格：悬念/冲突，默认悬念"},
        "hook_type": {"type": "string", "description": "3秒钩子类型，默认悬念"},
        "hook_variant": {"type": "string", "description": "A/B 钩子实验变体，空则不指定变体；A=数字+反常 B=提问+代入 C=悬念+冲突"},
        "pattern_file": {"type": "string", "description": "winning_patterns.json 路径，空则读默认，注入 winning_hooks"},
        "model": {"type": "string", "description": "模型名，默认 qwen3:creative"},
    }

    async def execute(
        self,
        topic,
        series="宇宙奥秘",
        style="悬念",
        hook_type="悬念",
        hook_variant="",
        pattern_file="",
        model="",
        **_,
    ):
        if not topic:
            logger.warning("mlx_script empty topic")
            return json.dumps({"error": "empty topic"})
        model = model or config.DEFAULT_CHAT_MODEL
        ensure_dirs()
        # 业务参数实时读配置 (修 A1), 不依赖模块级绑定值.
        scfg = _script_cfg()
        max_scenes = int(scfg.get("max_scenes", MAX_SCENES))
        variant_hook = scfg.get("hook_variants", VARIANT_HOOK)
        variant_hint = variant_hook.get(hook_variant, "")
        win_hint = self._load_winning_hooks(pattern_file or PATTERNS_FILE)
        user_msg = (
            f"系列：{series}\n风格：{style}\n钩子类型：{hook_type}\n主题：{topic}\n"
            f"请生成 3-4 个分镜的吸粉脚本。"
        )
        if variant_hint:
            user_msg += f"\n本条为 A/B 实验变体 {hook_variant}，首句钩子必须采用：{variant_hint}。"
        if win_hint:
            user_msg += f"\n爆款钩子回灌：{win_hint}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.9,
            "max_tokens": 1024,
        }
        try:
            async with httpx.AsyncClient(timeout=config.MLX_TIMEOUT, headers=mlx_headers()) as client:
                resp = await client.post(
                    f"{config.MLX_BASE}/v1/chat/completions", json=payload
                )
            if resp.status_code != 200:
                logger.error("mlx_script http %s: %s", resp.status_code, resp.text[:200])
                return json.dumps({"error": f"http {resp.status_code}"})
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = self._extract_json(content)
            if parsed is None:
                logger.error("mlx_script parse json failed: %s", content[:200])
                return json.dumps({"error": "json parse failed", "raw": content})
            scenes = parsed.get("scenes") or []
            if len(scenes) > max_scenes:
                logger.warning("mlx_script scenes=%d > max_scenes=%d, truncating", len(scenes), max_scenes)
                parsed["scenes"] = scenes[:max_scenes]
            logger.info("mlx_script ok title=%s scenes=%d variant=%s", parsed.get("title"), len(parsed.get("scenes", [])), hook_variant or "-")
            if hook_variant:
                parsed["hook_variant"] = hook_variant
            return json.dumps(parsed, ensure_ascii=False)
        except Exception as e:
            logger.exception("mlx_script failed")
            return json.dumps({"error": str(e)})

    @staticmethod
    def _extract_json(content):
        s = content.strip()
        if s.startswith("```"):
            s = s.split("```")[1]
            if s.startswith("json"):
                s = s[4:]
            s = s.strip()
        start = s.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            if depth > 0:
                tail = s[start:].rstrip()
                cands = [
                    tail + ("}" * depth),
                    tail[:-1] + "}",
                    tail + "}",
                ]
                seen = set()
                for cand in cands:
                    if cand in seen:
                        continue
                    seen.add(cand)
                    try:
                        return json.loads(cand)
                    except ValueError:
                        continue
            return None
        try:
            return json.loads(s[start : end + 1])
        except ValueError:
            return None

    @staticmethod
    def _load_winning_hooks(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError, FileNotFoundError):
            return ""
        if not isinstance(data, dict):
            return ""
        hooks = data.get("winning_hooks", [])
        if not hooks:
            return ""
        return "优先采用钩子模式：" + "、".join(str(x) for x in hooks[:4])
