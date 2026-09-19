"""时间工具：所有时刻以 UTC 存储，展示与节假日判断按当事人时区。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now_utc():
    return datetime.now(timezone.utc)


def parse_instant(value):
    """解析 ISO-8601 时刻；无时区后缀时按 UTC 处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_zone(dt, zone):
    return dt.astimezone(ZoneInfo(zone))


def format_instant(dt, zone=None):
    """输出 ISO 字符串；zone 给定时转该时区展示。"""
    if dt is None:
        return None
    if zone:
        dt = to_zone(dt, zone)
    return dt.isoformat()


def local_date(dt, zone):
    """UTC 时刻在某时区对应的日历日，用于节假日判断。"""
    return to_zone(dt, zone).date()


class SystemClock:
    def now(self):
        return now_utc()


class FixedClock:
    """测试时钟：手工推进，保证并发/排程演练可复现。"""

    def __init__(self, start=None):
        self._now = parse_instant(start) if start else now_utc()

    def now(self):
        return self._now

    def advance(self, **kwargs):
        from datetime import timedelta

        self._now += timedelta(**kwargs)
        return self._now

    def set(self, value):
        self._now = parse_instant(value)
        return self._now
