"""交期预测：受人员技能、并行工序、节假日与返工影响。

模型（对首批上线足够且可核查）：
- 工序 DAG：备料 → 刺绣组（苏绣/湘绣/钉珠可并行，各需不同技能工匠）
  → 裁剪 → 缝制 → 门店试衣缓冲 → 交付。
- 技能池：同一技能的并行任务在在册工匠间贪心分配；池中无人则该工序不可行。
- 工作日历：绣娘/裁缝按工坊日历（中国节假日），试衣按门店日历（英国 bank holiday）；
  落在节假日的工期顺延，并在结果中列出撞期节假日。
- 已完成工序以实际完成日落锤；未完成工序从 as_of 起按基期排；返工任务在原工序后
  追加同工艺工期。
- 待再确认变更会冻结生产；estimate_change 给出若再确认，最早可承诺的新日期。
"""

from datetime import date, timedelta

from .order import CRAFT_STAGE, STAGES
from .timeutil import parse_iso

FITTING_WORKDAYS = 2   # 门店试衣与微调（门店日历）
DELIVERY_WORKDAYS = 1   # 终检与交付准备

SKILL_OF = {"material": "material", "cut": "tailor", "tailor": "tailor"}


def skill_of(craft):
    return SKILL_OF.get(craft, craft)


def _schedule_span(calendar, start, days):
    """从 start 起占用 days 个工作日，返回(结束日, 撞期节假日[(date,name)])。"""
    cursor = start
    hits = []
    while not calendar.is_working(cursor):
        name = calendar.holiday_name(cursor)
        if name:
            hits.append((cursor, name))
        cursor += timedelta(days=1)
    used = 0
    while used < days:
        if calendar.is_working(cursor):
            used += 1
        else:
            name = calendar.holiday_name(cursor)
            if name:
                hits.append((cursor, name))
        if used < days:
            cursor += timedelta(days=1)
    return cursor, hits


class Scheduler:
    def __init__(self, directory):
        self.dir = directory

    # ---- 计划元素 ----
    def _stage_valid_from(self, state):
        """当前冻结版本要求重做的最早阶段；早于此阶段的旧批次/工序沿用。"""
        return state["confirmed"].get("redo_from", "material")

    def _old_work_valid(self, state, stage):
        """早于重做起点的旧批次/工序在当前冻结版本下仍有效。"""
        return STAGES.index(stage) < STAGES.index(self._stage_valid_from(state))

    def _planned_crafts(self, state):
        """当前冻结稿版要求的工艺，按 DAG 阶段排序去重。"""
        f = state["confirmed"]
        design = state["designs"][f["design_revision"]]
        crafts = list(dict.fromkeys(design.get("required_crafts") or
                                    ["material", "suxiu", "cut", "tailor"]))
        order = {"material": 0, "suxiu": 1, "xiangxiu": 1, "beadwork": 1,
                 "cut": 2, "tailor": 3}
        return sorted(crafts, key=lambda c: order.get(c, 9))

    def _tasks_for_craft(self, state, craft):
        # 重做起点之前的阶段沿用旧版本工序；之后只看当前冻结版本
        if self._old_work_valid(state, CRAFT_STAGE[craft]):
            return [t for t in state["tasks"] if t["craft"] == craft]
        seq = state["confirmed"]["seq"]
        return [t for t in state["tasks"]
                if t["craft"] == craft and t.get("frozen_seq") == seq]

    def _item_outcome(self, state, craft):
        """返回 (status, end_date)：done=已收口（原工序完成或返工完成）。"""
        tasks = self._tasks_for_craft(state, craft)
        if not tasks:
            return "unassigned", None
        originals = [t for t in tasks if not t.get("parent_task_id")]
        target = originals[-1] if originals else tasks[-1]
        if target["status"] == "completed":
            return "done", parse_iso(target["completed_at"]).date()
        if target["status"] == "rejected":
            reworks = [t for t in tasks if t.get("parent_task_id") == target["id"]]
            finished = [t for t in reworks if t["status"] == "completed"]
            if finished:
                return "done", parse_iso(finished[-1]["completed_at"]).date()
            return "rework_open", None
        return "open", None

    def _material_outcome(self, state):
        """备料由工坊主管登记批次完成，不占用绣娘/裁缝技能池。

        返回 (status, end_date)：重做起点之前可沿用旧批次，否则要当前版本批次。
        """
        seq = state["confirmed"]["seq"]
        allow_old = self._old_work_valid(state, "material")
        ready = [m for m in state["materials"]
                 if m["status"] == "ready" and (allow_old or m.get("frozen_seq") == seq)]
        if ready:
            return "done", parse_iso(ready[-1]["at"]).date()
        return "open", None

    # ---- 主预测 ----
    def predict(self, state, as_of=None):
        if state["confirmed"] is None:
            return {"feasible": False, "reason": "订单尚未确认，无制作版本"}
        as_of = (parse_iso(as_of) if as_of else None)
        today = as_of.date() if as_of else parse_iso(state["confirmed"]["at"]).date()
        cal = self.dir.calendars[self.dir.workshop_calendar_id]
        store = self.dir.stores[state["store_id"]]
        store_cal = self.dir.calendars[store["calendar_id"]]

        crafts = self._planned_crafts(state)
        groups = [
            ("material", [c for c in crafts if CRAFT_STAGE[c] == "material"]),
            ("embroidery", [c for c in crafts if CRAFT_STAGE[c] == "embroidery"]),
            ("cutting", [c for c in crafts if c == "cut"]),
            ("sewing", [c for c in crafts if c == "tailor"]),
        ]

        nodes = []
        warnings = []
        holidays_hit = []
        cursor = today
        for stage_name, members in groups:
            if not members:
                continue
            group_start = cursor
            ends = []
            # 技能池贪心：同一技能多个任务时在该技能工匠间排队
            free_after = {}

            # 备料不走绣娘/裁缝技能池（主管登记批次）
            if stage_name == "material":
                craft = members[0]
                status, end_date = self._material_outcome(state)
                if status == "done":
                    nodes.append({"stage": "material", "craft": craft, "skill": "material",
                                  "actual_end": end_date.isoformat(), "done": True,
                                  "feasible": True})
                    ends.append(end_date)
                else:
                    days = self.dir.crafts[craft]["base_days"]
                    end, hits = _schedule_span(cal, group_start, days)
                    holidays_hit.extend(hits)
                    ends.append(end)
                    nodes.append({"stage": "material", "craft": craft, "skill": "material",
                                  "start": group_start.isoformat(), "end": end.isoformat(),
                                  "working_days": days, "feasible": True,
                                  "owner": "工坊主管备料",
                                  "holidays": [d.isoformat() + " " + n for d, n in hits]})
                cursor = max(ends) + timedelta(days=1)
                continue

            for craft in members:
                skill = skill_of(craft)
                pool = self.dir.artisans_with_skill(skill)
                if not pool:
                    warnings.append(f"无具备 {skill} 技能的在册绣娘/裁缝，{craft} 工序无法启动")
                    nodes.append({"stage": stage_name, "craft": craft, "skill": skill,
                                  "feasible": False, "pool_size": 0})
                    continue
                if len(pool) == 1:
                    warnings.append(f"{skill} 技能仅 {pool[0]['name']} 一人，该工序无并行备份")
                for p in pool:
                    free_after.setdefault(p["id"], group_start)
                status, end_date = self._item_outcome(state, craft)
                if status == "done":
                    done_task = [t for t in self._tasks_for_craft(state, craft)
                                 if t["status"] == "completed"][-1]
                    chosen_id = done_task["assignee"]
                    nodes.append(self._done_node(stage_name, craft, skill, state, end_date))
                    ends.append(end_date)
                    free_after.setdefault(chosen_id, group_start)
                    free_after[chosen_id] = max(free_after[chosen_id], end_date)
                    continue
                # 待做（含返工）：选最早空闲工匠
                chosen = min(pool, key=lambda p: free_after[p["id"]])
                start = free_after[chosen["id"]]
                days = self.dir.crafts[craft]["base_days"]
                end, hits = _schedule_span(cal, start, days)
                holidays_hit.extend(hits)
                free_after[chosen["id"]] = end + timedelta(days=1)
                ends.append(end)
                nodes.append({
                    "stage": stage_name, "craft": craft, "skill": skill,
                    "assignee_planned": chosen["id"], "assignee_name": chosen["name"],
                    "start": start.isoformat(), "end": end.isoformat(),
                    "working_days": days, "parallel_group": stage_name,
                    "rework": status == "rework_open", "feasible": True,
                    "holidays": [d.isoformat() + " " + n for d, n in hits],
                })
            if ends:
                cursor = max(ends) + timedelta(days=1)

        feasible_nodes = [n for n in nodes if n.get("feasible", True)]
        infeasible = [n for n in nodes if n.get("feasible") is False]

        fitting = None
        predicted = cursor - timedelta(days=1) if nodes else None
        if predicted is not None and not infeasible:
            fit_end, fit_hits = _schedule_span(store_cal, cursor, FITTING_WORKDAYS)
            ready, ready_hits = _schedule_span(store_cal,
                                               fit_end + timedelta(days=1), DELIVERY_WORKDAYS)
            holidays_hit.extend(fit_hits + ready_hits)
            fitting = {"start": cursor.isoformat(), "end": fit_end.isoformat(),
                       "calendar": store["calendar_id"]}
            predicted = ready

        pending = next((c for c in state["changes"] if c["status"] == "proposed"), None)
        result = {
            "feasible": not infeasible,
            "as_of": (as_of.isoformat() if as_of else state["confirmed"]["at"]),
            "nodes": nodes,
            "fitting": fitting,
            "predicted_ready_date": predicted.isoformat() if predicted else None,
            "promised_date": state["confirmed"]["promised_date"],
            "on_time": (predicted.isoformat() <= state["confirmed"]["promised_date"]
                        if predicted and not infeasible else None),
            "holidays_on_path": sorted({d.isoformat() + " " + n for d, n in holidays_hit}),
            "warnings": sorted(set(warnings)),
            "blocked_by_change": None,
        }
        if pending:
            result["blocked_by_change"] = {
                "change_id": pending["id"], "kind": pending["kind"],
                "reasons": pending["reasons"],
                "recommendation": self.estimate_change(state, pending, as_of),
            }
        return result

    def _done_node(self, stage_name, craft, skill, state, end_date):
        task = [t for t in self._tasks_for_craft(state, craft)
                if t["status"] == "completed"][-1]
        return {"stage": stage_name, "craft": craft, "skill": skill,
                "assignee_name": self.dir.people[task["assignee"]]["name"],
                "actual_end": end_date.isoformat(), "done": True, "feasible": True}

    # ---- 变更影响估算 ----
    def estimate_change(self, state, change, as_of=None):
        """再确认后最早可承诺日期：当前预测 + 变更追加的工作日。"""
        base = self.predict(state, as_of)
        if not base["feasible"]:
            return {"feasible": False, "reason": base.get("reason", "当前计划不可行")}
        cal = self.dir.calendars[self.dir.workshop_calendar_id]
        store = self.dir.stores[state["store_id"]]
        store_cal = self.dir.calendars[store["calendar_id"]]
        start = date.fromisoformat(base["predicted_ready_date"])
        cursor = start + timedelta(days=1)
        hits = []

        kind = change["kind"]
        if kind == "material_scrap":
            days = self.dir.crafts["material"]["base_days"]
            cursor, hits = _schedule_span(cal, cursor, days)
            extra = f"重新备料检验 {days} 个工作日"
        elif kind == "design_change":
            target = change.get("target_revision")
            new_crafts = state["designs"][target]["required_crafts"] if target else []
            emb = [c for c in new_crafts if CRAFT_STAGE.get(c) == "embroidery"]
            tail = [c for c in new_crafts if c in ("cut", "tailor")]
            # 刺绣组并行取最长，裁剪缝制顺序叠加
            emb_days = max([self.dir.crafts[c]["base_days"] for c in emb], default=0)
            tail_days = sum(self.dir.crafts[c]["base_days"] for c in tail)
            total = emb_days + tail_days
            cursor, h1 = _schedule_span(cal, cursor, total) if total else (cursor - timedelta(days=1), [])
            hits = h1
            extra = f"新稿版重做刺绣/裁缝约 {total} 个工作日（刺绣并行取最长 {emb_days}）"
        elif kind == "measurement_change":
            days = self.dir.crafts["tailor"]["base_days"]
            cursor, hits = _schedule_span(cal, cursor, days)
            extra = f"按复测尺寸重裁重缝约 {days} 个工作日"
        elif kind == "fitting_late":
            cursor, h1 = _schedule_span(cal, cursor, 3)
            cursor, h2 = _schedule_span(store_cal, cursor + timedelta(days=1), 1)
            hits = h1 + h2
            extra = "裁缝档期重排 3 个工作日 + 重新试衣 1 个门店工作日"
        else:
            extra = "变更影响未建模"

        return {
            "feasible": True,
            "extra_work": extra,
            "earliest_new_date": cursor.isoformat(),
            "proposed_fee_addition": change.get("fee_addition", 0),
            "currency": change.get("currency"),
            "holidays_hit": sorted({d.isoformat() + " " + n for d, n in hits}),
        }
