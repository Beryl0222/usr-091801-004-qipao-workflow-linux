"""交期预测：工作日历、技能系数、并行工序与节假日共同决定交期。

排程为前向列表调度：
- 每道工序只能在其依赖完工后开始；
- 刺绣与裁剪依赖相同、互不依赖，因此并行，缝制须等二者都完工；
- 工匠容量为 1，同一工匠被占用时后续工序自动串行；
- 持续时间 = 标准工作日 / 技能系数 × 绣种系数，按工匠所在地日历
  在每日 09:00–17:00（8 工时）内逐分钟推进，自动跳过周末与节假日；
- 已确认变更单的额外工期叠加到受影响工序，返工按追加 attempt 重计工期。
输出含每道工序排程明细、关键路径、节假日与延期归因，可直接向顾客解释。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .catalog import (
    CALENDARS,
    EMBROIDERY_STYLES,
    HOLIDAYS,
    PROCESS_BY_STAGE,
    PROCESS_TEMPLATE,
    REGISTRY,
    STAGE_ROLES,
)
from .clock import format_instant, now_utc, parse_instant, to_zone

WORK_HOURS_PER_DAY = 8
WINDOW_START = 9 * 60            # 09:00，以当地午夜起分钟计
WINDOW_END = 17 * 60             # 17:00


def _is_workday(d, calendar_key):
    cal = CALENDARS[calendar_key]
    return d.weekday() not in cal["weekend"] and d not in HOLIDAYS.get(calendar_key, {})


def _holiday_name(d, calendar_key):
    return HOLIDAYS.get(calendar_key, {}).get(d)


def _at_window(d, minute_of_day, zone):
    return datetime(d.year, d.month, d.day,
                    minute_of_day // 60, minute_of_day % 60, tzinfo=zone)


def advance_work_time(start_utc, work_days, calendar_key):
    """从 start_utc 起消耗 work_days 个工作日（可为小数，按分钟取整）。

    返回 (snapped_start_utc, finish_utc, skipped)；起始落在非工作时段时
    移到下一个工作时段开头，沿途跨越的周末/节假日全部记入 skipped。
    """
    zone = ZoneInfo(CALENDARS[calendar_key]["zone"])
    cur = start_utc.astimezone(zone)
    remaining = int(round(work_days * WORK_HOURS_PER_DAY * 60))
    skipped = []

    def move_to_next_workday(dt):
        d = dt.date() + timedelta(days=1) if dt.hour * 60 + dt.minute >= WINDOW_END \
            else dt.date()
        while not _is_workday(d, calendar_key):
            if not any(s["date"] == d.isoformat() for s in skipped):
                skipped.append({"date": d.isoformat(),
                                "reason": _holiday_name(d, calendar_key) or "周末"})
            d += timedelta(days=1)
        return _at_window(d, WINDOW_START, zone)

    # 先把起点对齐到工作时段窗口
    while True:
        d = cur.date()
        if not _is_workday(d, calendar_key):
            if not any(s["date"] == d.isoformat() for s in skipped):
                skipped.append({"date": d.isoformat(),
                                "reason": _holiday_name(d, calendar_key) or "周末"})
            cur = _at_window(d + timedelta(days=1), WINDOW_START, zone)
            continue
        minute = cur.hour * 60 + cur.minute
        if minute < WINDOW_START:
            cur = _at_window(d, WINDOW_START, zone)
        elif minute >= WINDOW_END:
            cur = move_to_next_workday(cur)
            continue
        break
    snapped_start = cur

    while remaining > 0:
        d = cur.date()
        minute = cur.hour * 60 + cur.minute
        available = WINDOW_END - minute
        take = min(available, remaining)
        remaining -= take
        cur += timedelta(minutes=take)
        if remaining > 0:
            cur = move_to_next_workday(cur)

    return (snapped_start.astimezone(start_utc.tzinfo),
            cur.astimezone(start_utc.tzinfo), skipped)


class Scheduler:
    def __init__(self, clock=None, people=None):
        self.clock = clock
        self.people = people or REGISTRY["people"]

    # -- 工期 ---------------------------------------------------------------

    def _stage_work_days(self, order, stage, assignee):
        template = PROCESS_BY_STAGE[stage]
        p = self.people[assignee]
        factor = float(p.skills.get(template["skill"], 0.0)) or 0.7
        days = float(template["work_days"]) / factor
        if stage == "embroidery" and order.current_version:
            style = EMBROIDERY_STYLES.get(order.current_version.embroidery_style)
            if style:
                days *= style["factor"]
        change_refs = []
        for co in order.change_orders:
            if co.status == "confirmed" and co.affected_stage == stage:
                days += co.extra_work_days
                change_refs.append(co.id)
        # 待返工：每个在制返工 attempt 再消耗一遍该工序工期
        pending_rework = [t for t in order.tasks
                          if t.stage == stage and t.rework_of is not None
                          and t.status in ("assigned", "started")]
        if pending_rework:
            days += len(pending_rework) * float(template["work_days"]) / factor
        return round(days, 3), change_refs

    def _candidates(self, stage):
        roles = STAGE_ROLES[stage]
        return [pid for pid, p in self.people.items() if p.role in roles]

    @staticmethod
    def _latest_attempt(order, stage):
        attempts = [t for t in order.tasks if t.stage == stage]
        return max(attempts, key=lambda t: t.attempt, default=None)

    # -- 主排程 -------------------------------------------------------------

    def predict(self, order, *, include_pending_changes=False):
        start = self.clock.now() if self.clock else now_utc()
        scheduled = {}
        busy_until = {}          # person_id -> datetime
        busy_stage = {}          # person_id -> 占用其容量的工序
        pending = [co for co in order.change_orders if co.status == "proposed"]

        for template in PROCESS_TEMPLATE:
            stage = template["stage"]
            latest = self._latest_attempt(order, stage)
            if latest is not None and latest.status == "done":
                finish_dt = parse_instant(latest.finished_at)
                scheduled[stage] = {
                    "stage": stage, "name": template["name"],
                    "assignee": latest.assignee, "status": "done",
                    "start": None, "finish": latest.finished_at,
                    "finish_dt": finish_dt, "work_days": 0.0,
                    "calendar": None, "skipped": [], "change_orders": [],
                    "predecessor": None,
                }
                continue

            dep_ready = []  # (dep_stage, finish_dt)
            for dep in template["deps"]:
                dep_item = scheduled[dep]
                dep_ready.append((dep, dep_item["finish_dt"]))
            deps_start = max([start] + [dt for _, dt in dep_ready])

            best = None
            for pid in self._candidates(stage):
                p = self.people[pid]
                eff_start = max(deps_start, busy_until.get(pid, deps_start))
                work_days, refs = self._stage_work_days(order, stage, pid)
                if include_pending_changes:
                    work_days += sum(co.extra_work_days for co in pending
                                     if co.affected_stage == stage)
                snapped, finish_dt, skipped = advance_work_time(
                    eff_start, work_days, p.calendar)
                candidate = (pid, snapped, finish_dt, work_days, skipped, refs)
                if best is None or finish_dt < best[2]:
                    best = candidate

            pid, eff_start, finish_dt, work_days, skipped, refs = best
            # 关键链前驱：资源占用晚于依赖则记资源占用工序，否则记最晚依赖
            if pid in busy_until and busy_until[pid] > deps_start \
                    and eff_start == busy_until[pid]:
                predecessor = busy_stage[pid]
                predecessor_kind = "resource"
            else:
                predecessor = max(dep_ready, key=lambda x: x[1], default=(None, None))[0]
                predecessor_kind = "dependency"
            busy_until[pid] = finish_dt
            busy_stage[pid] = stage

            scheduled[stage] = {
                "stage": stage, "name": template["name"], "assignee": pid,
                "status": "planned",
                "start": format_instant(eff_start), "finish": format_instant(finish_dt),
                "finish_dt": finish_dt, "work_days": round(work_days, 2),
                "calendar": self.people[pid].calendar, "skipped": skipped,
                "change_orders": refs,
                "predecessor": predecessor, "predecessor_kind": predecessor_kind,
            }

        # 关键路径：由交付沿前驱回溯
        critical = []
        cur_stage = "delivery"
        while cur_stage:
            critical.append(cur_stage)
            cur_stage = scheduled[cur_stage]["predecessor"]
        critical.reverse()

        stages_out = []
        for template in PROCESS_TEMPLATE:
            item = dict(scheduled[template["stage"]])
            item.pop("finish_dt", None)
            stages_out.append(item)

        all_skipped = {}
        for item in stages_out:
            for s in item["skipped"]:
                all_skipped[s["date"]] = s
        delivery_finish = scheduled["delivery"]["finish_dt"]
        zone = _customer_zone(order)

        return {
            "order_id": order.id,
            "predicted_at": format_instant(start),
            "predicted_finish": format_instant(delivery_finish),
            "customer_zone": zone,
            "predicted_finish_local": format_instant(delivery_finish, zone),
            "stages": stages_out,
            "critical_path": critical,
            "holidays_observed": [all_skipped[k] for k in sorted(all_skipped)],
            "delays": [
                {
                    "reason": d.reason, "days": d.days, "source": d.source,
                    "ref": d.ref, "confirmed_by": d.confirmed_by,
                    "confirmed_at": d.confirmed_at, "detail": d.detail,
                }
                for d in order.delays
            ],
            "pending_changes": [
                {"id": co.id, "reason": co.reason,
                 "extra_work_days": co.extra_work_days,
                 "cost_delta": co.cost_delta, "summary": co.summary}
                for co in pending
            ],
        }


def _customer_zone(order):
    cust = REGISTRY["people"].get(order.customer_id)
    sid = cust.store_id if cust else None
    return REGISTRY["stores"][sid]["zone"] if sid else "UTC"
