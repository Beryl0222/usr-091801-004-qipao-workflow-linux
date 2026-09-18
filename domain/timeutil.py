"""时间工具：门店以顾客所在时区记录确认时刻。

所有事件统一存 UTC（ISO 字符串带 Z），展示与"跨时区确认"时再按
IANA 时区（如 Europe/London、Asia/Shanghai）换算。
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def now_utc():
    return datetime.now(UTC)


def to_iso(dt):
    """aware datetime -> 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'。"""
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区信息")
    return dt.isoformat()


def parse_iso(value):
    """解析 ISO 时间；裸 Z 结尾转换为 +00:00。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_str(dt, tz_name):
    """把 UTC 时刻格式化为顾客所在时区的 'YYYY-MM-DD HH:MM 时区'。"""
    tz = ZoneInfo(tz_name)
    local = parse_iso(dt).astimezone(tz)
    label = tz_name.split("/")[-1].replace("_", " ")
    return local.strftime("%Y-%m-%d %H:%M ") + label


def date_in_zone(dt, tz_name):
    """UTC 时刻在顾客时区落在哪一个当地日期。"""
    return parse_iso(dt).astimezone(ZoneInfo(tz_name)).date()
