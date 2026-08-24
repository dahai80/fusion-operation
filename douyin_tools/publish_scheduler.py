import datetime
import json
import logging

from .config import get_json_config
from tools.base import BaseTool

logger = logging.getLogger(__name__)


def _parse_peak_windows(raw):
    # 声明式配置: scheduler.peak_windows 形如 ["12-13","19-21"] -> [(12,13),(19,21)].
    out = []
    for item in raw or []:
        try:
            s, e = str(item).split("-")
            out.append((int(s), int(e)))
        except (ValueError, AttributeError):
            logger.warning("publish_scheduler: bad peak window %r, skip", item)
    # 排序保证 _next_window 的"取首个未到窗口"假设成立 (修 L5: 无序配置会漏选下一个窗口).
    return sorted(out, key=lambda x: x[0])


PEAK_WINDOWS = _parse_peak_windows(
    get_json_config().get("scheduler", {}).get("peak_windows", ["12-13", "19-21"])
) or [(12, 13), (19, 21)]

PEAK_LABEL = {
    "09-10": "早高峰",
    "11-12": "午高峰",
    "14-15": "午后高峰",
    "18-19": "晚高峰",
    "12-13": "午休高峰",
    "19-21": "晚高峰",
}


class PublishSchedulerTool(BaseTool):
    name = "publish_scheduler"
    description = "发布时段策略：检查当前是否在抖音活跃高峰(12-13/19-21)，输出 in_peak/next_window 供 Graph D 决定立即发或延后"
    parameters = {
        "now": {"type": "string", "description": "测试用指定时间 HH:MM，空则取当前时间"},
        "windows": {"type": "string", "description": "自定义高峰窗口，逗号分隔 HH-HH，空则用默认 12-13,19-21"},
    }

    async def execute(self, now="", windows="", **_):
        hh_mm = now.strip() or datetime.datetime.now().strftime("%H:%M")
        # 每次执行实时读配置 (修 A1: 改 peak_windows 不重启 daemon 也生效).
        if windows:
            wins = self._parse_windows(windows)
        else:
            wins = _parse_peak_windows(
                get_json_config().get("scheduler", {}).get("peak_windows", ["12-13", "19-21"])
            ) or PEAK_WINDOWS
        cur_min = self._to_minutes(hh_mm)
        in_peak, peak_label = self._in_peak(cur_min, wins)
        next_window = self._next_window(cur_min, wins)
        out = {
            "status": "scheduled",
            "now": hh_mm,
            "in_peak": "true" if in_peak else "false",
            "peak_label": peak_label,
            "next_window": next_window,
            "should_wait": "false" if in_peak else "true",
            "windows": [f"{s:02d}-{e:02d}" for s, e in wins],
        }
        logger.info(
            "publish_scheduler now=%s in_peak=%s next=%s should_wait=%s",
            hh_mm, out["in_peak"], next_window, out["should_wait"],
        )
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _parse_windows(raw):
        out = []
        for part in raw.split(","):
            part = part.strip()
            if "-" not in part:
                continue
            try:
                s, e = part.split("-")
                out.append((int(s), int(e)))
            except ValueError:
                continue
        out = sorted(out, key=lambda x: x[0]) if out else []
        return out or PEAK_WINDOWS

    @staticmethod
    def _to_minutes(hh_mm):
        try:
            h, m = hh_mm.split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return 0

    @staticmethod
    def _in_peak(cur, wins):
        for s, e in wins:
            if s * 60 <= cur < e * 60:
                label = PEAK_LABEL.get(f"{s:02d}-{e:02d}", f"{s}-{e}高峰")
                return True, label
        return False, ""

    @staticmethod
    def _next_window(cur, wins):
        for s, e in wins:
            if cur < s * 60:
                return f"{s:02d}:00"
        if wins:
            return f"{wins[0][0]:02d}:00"
        return ""
