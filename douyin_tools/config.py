import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get(
    "DOUYIN_CONFIG_FILE",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "douyin_config.json",
    ),
)


def _load_json_config():
    # 声明式配置层: 用户改 douyin_config.json 即可调整策略, 不改代码.
    # 优先级: 环境变量 > JSON 配置 > 硬编码默认 (调用方按此顺序取舍).
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("config: loaded %s keys=%s", CONFIG_FILE, list(data.keys()))
        return data
    except FileNotFoundError:
        logger.warning("config: %s not found, fallback to env+defaults", CONFIG_FILE)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.error(
            "config: failed to read %s: %s, fallback to env+defaults", CONFIG_FILE, e
        )
        return {}


def _cfg_mtime():
    # OSError (文件不存在/权限拒绝) 返回 None 而非 0.0 (审计 R7).
    # 旧返回 0.0 被 _refresh_if_stale 的 `if m and ...` 当 falsy 短路 -> 配置文件丢失后永不刷新.
    try:
        return os.path.getmtime(CONFIG_FILE)
    except OSError:
        return None


_JSON_CFG = _load_json_config()
_JSON_CFG_MTIME = _cfg_mtime()


def reload_json_config():
    # 强制重读 douyin_config.json (CLI 入口可显式调). 运行时靠 get_json_config 的 mtime 失效缓存自动感知.
    global _JSON_CFG, _JSON_CFG_MTIME
    _JSON_CFG = _load_json_config()
    _JSON_CFG_MTIME = _cfg_mtime()
    return _JSON_CFG


def _refresh_if_stale():
    # mtime 失效缓存: daemon launchd 长进程不重启, 改 douyin_config.json 后下次读即生效.
    # mtime 不变则返回缓存 (避免每次 LLM 调用读盘).
    # 修 A1 根因: 旧实现 import 时读一次永不刷新 -> daemon cron 改配置不生效 (run_graph 注释"cron 起新进程"虚假).
    global _JSON_CFG, _JSON_CFG_MTIME
    m = _cfg_mtime()
    # 用 is not None 判空而非 truthy (审计 R7): 旧 `if m` 把 None/0.0 当 falsy 短路,
    # 配置文件丢失/初装缺失时永不刷新. is not None 只在文件不存在时跳过 (保留上次缓存, 不wipe到默认),
    # 文件存在 (m 为真实 mtime) 则按 mtime 变化刷新.
    if m is not None and m != _JSON_CFG_MTIME:
        logger.info("config: mtime changed, reloading %s", CONFIG_FILE)
        _JSON_CFG = _load_json_config()
        _JSON_CFG_MTIME = m


def _cfg(section, key, default):
    _refresh_if_stale()
    return _JSON_CFG.get(section, {}).get(key, default)


def get_json_config():
    # 暴露原始 JSON 配置供工具读取非 config.py 常量的业务参数
    # (hook_variants / peak_windows / 阈值等, 上游 issue #125 落地后映射进 manifest tool.config).
    _refresh_if_stale()
    return _JSON_CFG


# ---------------------------------------------------------------------------
# 配置常量分两类 (审计 H5 修复):
#
# [进程生命周期稳定] 路径/视频参数: 改 JSON 不影响已开文件句柄, 需重启进程生效.
#   保留模块级求值 (import 时快照). daemon launchd 长进程不重启改这些不生效, 已在
#   douyin_config.json + README 注明. 这些值运行中变更会损坏文件句柄/编码管道, 不应动态.
#   -> VIDEO_DIR/AUDIO_DIR/IMAGE_DIR/FALLBACK_IMAGE_DIR/DOUYIN_PROFILE/OPS_DATA_DIR/
#      QUEUE_DIR/TOPICS_FILE/COMFYUI_OUTPUT_DIR/COMFYUI_AUDIO_TEMP_DIR/AGENT_STUDIO_SOCKET/
#      FPS/W/H/FONT_PATH/MLX_SETTINGS_PATH/MLX_API_KEY/CELESTIAL_TERMS/DOUYIN_*_URL
#
# [动态生效] 模型 ID/服务端点/超时: daemon 长进程改 JSON 立即生效, 是 H5 核心.
#   用模块级 __getattr__ 动态属性: 工具调用点用 `config.MLX_BASE` (属性访问) 触发实时读取,
#   `from .config import MLX_BASE` 只 import 时绑定一次 (仍是快照) 故已迁调用点为属性访问.
#   环境变量优先级保留 (> JSON > 默认), 与原 os.environ.get 语义一致.
#
#   MLX_BASE/MLX_TIMEOUT/DEFAULT_CHAT_MODEL/DEFAULT_TTS_MODEL/DEFAULT_TTS_VOICE/
#   DEFAULT_IMAGE_MODEL/COMFYUI_BASE/COMFYUI_TIMEOUT/COMFYUI_IMAGE_MODEL/
#   COMFYUI_TTS_MODEL/COMFYUI_TTS_BACKEND/COMFYUI_TTS_HTTP_MODEL/COMFYUI_TTS_HTTP_VOICE/
#   NASA_API_BASE/NASA_TIMEOUT/WEB_PER_QUERY
# ---------------------------------------------------------------------------

# --- 进程生命周期稳定常量 (import 时快照, 改需重启) ---
MLX_API_KEY = os.environ.get("FUSION_MLX_API_KEY", "")
MLX_SETTINGS_PATH = os.path.expanduser(
    os.environ.get(
        "FUSION_MLX_SETTINGS",
        _cfg("mlx", "settings_path", "~/.fusion-mlx/settings.json"),
    )
)

VIDEO_DIR = os.environ.get(
    "DOUYIN_VIDEO_DIR",
    _cfg("paths", "video_dir", "/Users/dahai/fusion/fusion-operation/out/videos"),
)
AUDIO_DIR = os.environ.get(
    "DOUYIN_AUDIO_DIR",
    _cfg("paths", "audio_dir", "/Users/dahai/fusion/fusion-operation/out/audio"),
)
IMAGE_DIR = os.environ.get(
    "DOUYIN_IMAGE_DIR",
    _cfg("paths", "image_dir", "/Users/dahai/fusion/fusion-operation/out/images"),
)
FALLBACK_IMAGE_DIR = os.environ.get(
    "DOUYIN_FALLBACK_IMAGES",
    _cfg("paths", "fallback_image_dir", "/Users/dahai/video/images/hq"),
)

DOUYIN_PROFILE = os.environ.get(
    "DOUYIN_PROFILE",
    _cfg("paths", "douyin_profile", "/Users/dahai/video/douyin_profile"),
)
DOUYIN_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
DOUYIN_MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
DOUYIN_COMMENT_URL = "https://creator.douyin.com/creator-micro/interactive/comment"
DOUYIN_HOME_URL = "https://creator.douyin.com/creator-micro/home"
DOUYIN_ITEM_LIST_API = "https://creator.douyin.com/aweme/v1/creator/item/list"
OPS_DATA_DIR = os.environ.get(
    "DOUYIN_OPS_DATA_DIR",
    _cfg("paths", "ops_data_dir", "/Users/dahai/fusion/fusion-operation/out/ops"),
)
QUEUE_DIR = os.environ.get(
    "DOUYIN_QUEUE_DIR",
    _cfg("paths", "queue_dir", "/Users/dahai/fusion/fusion-operation/out/queue"),
)
# lease TTL 单源 (审计 0908 F2 ④): queue_ops dequeue + ops_health stale-inflight 告警同源读.
# default 1800 覆盖最坏发布耗时 (审计 P0-1); env DOUYIN_LEASE_TTL 可覆盖.
LEASE_TTL = int(os.environ.get("DOUYIN_LEASE_TTL", "1800"))
AGENT_STUDIO_SOCKET = os.environ.get(
    "FUSION_STUDIO_SOCKET",
    _cfg("agent_studio", "socket", "/tmp/fusion-studio.sock"),
)
TOPICS_FILE = os.environ.get(
    "DOUYIN_TOPICS_FILE",
    _cfg(
        "paths",
        "topics_file",
        "/Users/dahai/fusion/fusion-operation/out/queue/topics.json",
    ),
)

# ComfyUI output 目录 (folder_paths.get_output_directory), /history 返回 filename+subfolder.
COMFYUI_OUTPUT_DIR = os.environ.get(
    "FUSION_COMFYUI_OUTPUT_DIR",
    _cfg(
        "comfyui",
        "output_dir",
        "/Users/dahai/fusion/fusion-comfyui/ComfyUI/output",
    ),
)
# Fusion 节点把音频存到 tempfile.gettempdir()/fusion_comfyui_audio, 且不在 /history outputs 暴露.
# 业务层按 filename_prefix 在此目录回找. (上游 gap: 音频应走 ComfyUI output dir 进 /history)
COMFYUI_AUDIO_TEMP_DIR = os.path.join(
    os.environ.get("FUSION_COMFYUI_AUDIO_TEMP", os.environ.get("TMPDIR", "/tmp")),
    "fusion_comfyui_audio",
)

# 天体名词词典: 从 img_prompt 抽首个命中词作 NASA query (简短名词召回准).
_DEFAULT_CELESTIAL = [
    "jupiter", "mars", "earth", "saturn", "neptune", "uranus", "venus", "mercury",
    "moon", "sun", "solar", "nebula", "galaxy", "black hole", "milky way",
    "aurora", "comet", "asteroid", "supernova", "quasar", "pulsar", "star",
    "exoplanet", "cosmos", "orbit", "eclipse", "meteor", "dwarf planet",
    "red giant", "white dwarf", "neutron star", "accretion disk", "event horizon",
]
CELESTIAL_TERMS = _cfg("web_image", "celestial_terms", _DEFAULT_CELESTIAL)

FPS = int(_cfg("video", "fps", 24))
W = int(_cfg("video", "width", 1080))
H = int(_cfg("video", "height", 1920))
# E3: FONT_PATH 进配置. 默认空 -> 走系统字体回退链 (STHeiti -> PingFang).
# 非空用配置值 (换字体改 JSON 不改代码). ffmpeg_compose import 时绑定, 故静态常量.
_DEFAULT_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
if not os.path.exists(_DEFAULT_FONT):
    _DEFAULT_FONT = "/System/Library/Fonts/PingFang.ttc"
FONT_PATH = _cfg("video", "font_path", "") or _DEFAULT_FONT
if FONT_PATH != _DEFAULT_FONT and not os.path.exists(FONT_PATH):
    logger.warning("config font_path 不存在, 回退系统字体: %s", FONT_PATH)
    FONT_PATH = _DEFAULT_FONT

# --- 动态常量注册表 (审计 H5): __getattr__ 用. 调用点必须 config.XX 属性访问才动态 ---
# key -> (环境变量名, config section, config key, 默认值, 类型转换). None env/section 跳过该层.
_DYNAMIC_CONSTANTS = {
    "MLX_BASE": ("FUSION_MLX_BASE", "mlx", "base", "http://localhost:11434", str),
    "MLX_TIMEOUT": ("FUSION_MLX_TIMEOUT", "mlx", "timeout", 300, float),
    "DEFAULT_CHAT_MODEL": ("FUSION_CHAT_MODEL", "models", "chat", "Qwen3.5-9B-4bit", str),
    "DEFAULT_TTS_MODEL": ("FUSION_TTS_MODEL", "models", "tts", "kokoro", str),
    "DEFAULT_TTS_VOICE": ("FUSION_TTS_VOICE", "models", "tts_voice", "af_heart", str),
    "DEFAULT_IMAGE_MODEL": ("FUSION_IMAGE_MODEL", "models", "image", "flux-2", str),
    "COMFYUI_BASE": ("FUSION_COMFYUI_BASE", "comfyui", "base", "http://127.0.0.1:11445", str),
    "COMFYUI_TIMEOUT": ("FUSION_COMFYUI_TIMEOUT", "comfyui", "timeout", 300, float),
    "COMFYUI_IMAGE_MODEL": ("FUSION_COMFYUI_IMAGE_MODEL", "comfyui", "image_model", "flux2-klein-4b.safetensors", str),
    "COMFYUI_TTS_MODEL": ("FUSION_COMFYUI_TTS_MODEL", "comfyui", "tts_model", "mlx-community/Kokoro-82M-bf16", str),
    # 中文 TTS: Kokoro 是英文音色, 读中文产出乱语重复. Qwen3-TTS 支持中文, 走 HTTP backend
    # 复用 fusion-mlx daemon 已加载的模型 (in-process 加载争 Metal 资源卡死).
    # 上游 PR#26 给 FusionVoiceSynthesize 加 backend=http 可选输入.
    "COMFYUI_TTS_BACKEND": ("FUSION_COMFYUI_TTS_BACKEND", "comfyui", "tts_backend", "http", str),
    "COMFYUI_TTS_HTTP_MODEL": ("FUSION_COMFYUI_TTS_HTTP_MODEL", "comfyui", "tts_http_model", "Qwen3-TTS-12Hz-1.7B-Base-8bit", str),
    "COMFYUI_TTS_HTTP_VOICE": ("FUSION_COMFYUI_TTS_HTTP_VOICE", "comfyui", "tts_http_voice", "Cherry", str),
    # NASA Image Library 真实天体图搜索 (公共领域商用合规, Graph C 真实图优先路径).
    # 免 key REST API: images-api.nasa.gov/search -> item href metadata JSON -> large jpg.
    "NASA_API_BASE": ("NASA_API_BASE", "web_image", "nasa_api_base", "https://images-api.nasa.gov", str),
    "NASA_TIMEOUT": ("NASA_TIMEOUT", "web_image", "timeout", 20, float),
    "WEB_PER_QUERY": ("WEB_PER_QUERY", "web_image", "per_query", 1, int),
}


def _resolve_dynamic(name):
    # 审计 H5: 动态常量实时读 env > JSON config > 默认, 每次 config.XX 访问触发 (非 import 快照).
    # _refresh_if_stale 由 _cfg 触发, 保证 JSON 已按 mtime 刷新.
    env_name, section, key, default, caster = _DYNAMIC_CONSTANTS[name]
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        raw = _cfg(section, key, default)
    try:
        return caster(raw)
    except (ValueError, TypeError):
        logger.warning("config: 动态常量 %s cast 失败 raw=%r 用默认 %r", name, raw, default)
        return caster(default)


def __getattr__(name):
    # 审计 H5: 模块级动态属性. `config.MLX_BASE` (属性访问) 实时读取, daemon 长进程改 JSON 立即生效.
    # `from .config import MLX_BASE` 仍只 import 时绑定一次 (Python import 语义), 故调用点已迁属性访问.
    # 只对 _DYNAMIC_CONSTANTS 注册的名字生效, 其他名字正常抛 AttributeError.
    if name in _DYNAMIC_CONSTANTS:
        return _resolve_dynamic(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# settings.json api_key mtime 缓存: key 几乎不变, 每条造片链多次 mlx_headers() 调用
# 每次盘读 open+json.load 纯浪费. mtime 不变复用缓存, 变了才重读.
_MLX_KEY_CACHE = {"mtime": 0.0, "key": ""}


def _resolve_mlx_api_key():
    if MLX_API_KEY:
        return MLX_API_KEY
    try:
        m = os.path.getmtime(MLX_SETTINGS_PATH)
    except OSError:
        return _MLX_KEY_CACHE["key"]
    if m == _MLX_KEY_CACHE["mtime"] and _MLX_KEY_CACHE["key"]:
        return _MLX_KEY_CACHE["key"]
    try:
        with open(MLX_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("auth", {}).get("api_key", "")
        if key:
            _MLX_KEY_CACHE["mtime"] = m
            _MLX_KEY_CACHE["key"] = key
            return key
    except Exception as e:
        logger.debug("read mlx settings api_key failed: %s", e)
    return ""


def mlx_headers():
    key = _resolve_mlx_api_key()
    if key:
        return {
            "Authorization": f"Bearer {key}",
            "x-api-key": key,
            "X-Fusion-Route": "fusion-operation",
        }
    return {"X-Fusion-Route": "fusion-operation"}


def mlx_health():
    # 探测 mlx 推理引擎可达性 + 鉴权有效性. cron 自动化路径在 LLM 调用前先诊断,
    # 避免 daemon 携带 stale key 调 mlx 401 静默空输出 (A5).
    # 返回 {"ok": bool, "reason": str, "http": int, "model_count": int}.
    # 审计 H5: MLX_BASE 已是动态常量, 模块内裸名会 NameError (__getattr__ 只对外部属性访问生效).
    # 用 _resolve_dynamic 实时读, 同 config.MLX_BASE 语义.
    try:
        r = httpx.get(f"{_resolve_dynamic('MLX_BASE')}/v1/models", headers=mlx_headers(), timeout=10)
    except Exception as e:
        return {"ok": False, "reason": f"unreachable: {e}", "http": 0, "model_count": 0}
    if r.status_code == 401:
        return {"ok": False, "reason": "auth 401 (stale FUSION_MLX_API_KEY? unset env 重启 daemon)", "http": 401, "model_count": 0}
    if r.status_code == 403:
        return {"ok": False, "reason": "forbidden 403 (X-Fusion-Route 缺失?)", "http": 403, "model_count": 0}
    if r.status_code != 200:
        return {"ok": False, "reason": f"http {r.status_code}", "http": r.status_code, "model_count": 0}
    try:
        models = r.json().get("data", []) or []
    except ValueError:
        models = []
    return {"ok": True, "reason": "ok", "http": 200, "model_count": len(models)}


def ensure_dirs():
    for d in (VIDEO_DIR, AUDIO_DIR, IMAGE_DIR, OPS_DATA_DIR, QUEUE_DIR):
        os.makedirs(d, exist_ok=True)
    logger.info(
        "ensure_dirs done video=%s audio=%s image=%s ops=%s queue=%s",
        VIDEO_DIR,
        AUDIO_DIR,
        IMAGE_DIR,
        OPS_DATA_DIR,
        QUEUE_DIR,
    )


def setup_logging(level=logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
