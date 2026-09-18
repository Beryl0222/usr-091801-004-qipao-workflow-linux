"""订单聚合：事件折叠 + 命令决策（纯函数，无线程/IO）。

阶段（见 README）：
  inquiry 咨询 → design 设计 → material 备料 → embroidery 刺绣
  → cutting 裁剪 → sewing 缝制 → fitting 试衣 → delivered 交付

关键不变量：
1. 制作版本不可偷偷替换：确认时对稿版关键元素+量体版本+报价计算 SHA-256
   清单；稿版 revision 只增不改；改稿必须走变更单并由顾客再次确认。
2. 故事传播许可与制作授权分离：撤回传播不影响已确认制作与合同。
3. 收款、工时只随事件追加，任何命令都无法改写或删除。
4. 改稿/迟到/材料报废/复测变化产生变更单：新费用以"附加费"叠加、
   新交期待顾客再确认；再确认前已收款与已完成工时原封不动。
"""

import hashlib
import json as _json

from .errors import Conflict, NotFound, ValidationFailed
from .timeutil import parse_iso

STAGES = [
    "inquiry",
    "design",
    "material",
    "embroidery",
    "cutting",
    "sewing",
    "fitting",
    "delivered",
]

# 工序进入的阶段；beadwork 可与 embroidery 并行（scheduler 处理并行关系）
CRAFT_STAGE = {
    "material": "material",
    "suxiu": "embroidery",
    "xiangxiu": "embroidery",
    "beadwork": "embroidery",
    "cut": "cutting",
    "tailor": "sewing",
}

# 工匠任务可见的最小信息范围
TASK_SCOPE = {
    "material": ["fabric_spec"],
    "suxiu": ["motif_elements", "embroidery_area"],
    "xiangxiu": ["motif_elements", "embroidery_area"],
    "beadwork": ["motif_elements", "embroidery_area"],
    "cut": ["measurements", "pattern_elements"],
    "tailor": ["measurements", "pattern_elements"],
}

_TERMINAL = {"delivered", "cancelled"}


def _ev(etype, **data):
    return {"type": etype, "data": data}


def _quote_core(quote):
    """清单只固化价格与币种；明细变化不构成制作版本替换。"""
    return {"amount": quote["amount"], "currency": quote["currency"]}


def _manifest(design_revision, elements, measurement_version, quote):
    """制作版本清单：任何一项被替换都会改变哈希。"""
    payload = _json.dumps(
        {
            "design_revision": design_revision,
            "elements": elements,
            "measurement_version": measurement_version,
            "quote": _quote_core(quote),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fold(events):
    """把事件流折叠为订单状态。"""
    s = {
        "stage": None,
        "customer_id": None,
        "store_id": None,
        "tz": None,
        "opened_at": None,
        "story": None,
        "grants": {},          # scope -> {at, by}
        "withdrawn_scopes": {},  # scope -> {at, by}
        "designs": {},         # revision -> 稿版（不可变）
        "measurement_versions": [],
        "quote": None,
        "confirmed": None,     # 当前冻结的制作版本
        "frozen_history": [],  # 历次冻结版本（再确认生成新条）
        "payments": [],
        "materials": [],
        "tasks": [],
        "changes": [],
        "fittings": [],
        "delivered": None,
        "notes": [],
    }

    for e in events:
        t, d = e["event"], e["data"]
        if t == "OrderOpened":
            s.update(stage="inquiry", customer_id=d["customer_id"], store_id=d["store_id"],
                     tz=d.get("tz"), opened_at=d["at"])
        elif t == "NoteAdded":
            s["notes"].append({"at": d["at"], "by": d["by"], "text": d["text"]})

        elif t == "StoryRecorded":
            s["story"] = {
                "text": d["text"],
                "cultural_meaning": d.get("cultural_meaning", ""),
                "recorded_at": d["at"],
                "recorded_by": d["by"],
            }
        elif t == "StoryConsentGranted":
            s["grants"][d["scope"]] = {"at": d["at"], "by": d["by"]}
            s["withdrawn_scopes"].pop(d["scope"], None)
        elif t == "StoryConsentWithdrawn":
            s["grants"].pop(d["scope"], None)
            s["withdrawn_scopes"][d["scope"]] = {"at": d["at"], "by": d["by"]}

        elif t == "DesignSubmitted":
            s["designs"][d["revision"]] = {
                "revision": d["revision"],
                "by": d["by"],
                "at": d["at"],
                "elements": d["elements"],
                "required_crafts": d.get("required_crafts", []),
                "embroidery_area": d.get("embroidery_area", {}),
                "fabric_spec": d.get("fabric_spec", {}),
                "notes": d.get("notes", ""),
            }
        elif t == "MeasurementTaken":
            s["measurement_versions"].append(
                {"version": d["version"], "at": d["at"], "by": d["by"], "data": d["data"]}
            )
        elif t == "QuoteGiven":
            s["quote"] = {"amount": d["amount"], "currency": d["currency"],
                          "breakdown": d.get("breakdown", {}), "at": d["at"], "by": d["by"]}

        elif t == "OrderConfirmed":
            frozen = {
                "seq": d["seq"],
                "at": d["at"],
                "by": d["by"],
                "design_revision": d["design_revision"],
                "elements": d["elements"],
                "measurement_version": d["measurement_version"],
                "quote": d["quote"],
                "price": d["price"],
                "promised_date": d["promised_date"],
                "manifest": d["manifest"],
                "redo_from": d.get("redo_from", "material"),
            }
            s["confirmed"] = frozen
            s["frozen_history"].append(frozen)
            # 仅首次确认进入备料；再确认只更新冻结版本，阶段不倒退，
            # 除非事件显式要求回到重做起点（改稿后重做刺绣/裁剪）
            if d["seq"] == 1:
                s["stage"] = "material"
            elif d.get("reset_stage"):
                s["stage"] = d["reset_stage"]

        elif t == "PaymentRecorded":
            s["payments"].append({"id": d["id"], "amount": d["amount"], "currency": d["currency"],
                                  "at": d["at"], "by": d["by"], "purpose": d["purpose"]})

        elif t == "MaterialPrepared":
            s["materials"].append({"batch_id": d["batch_id"], "fabric": d["fabric"],
                                   "length_m": d["length_m"], "at": d["at"], "by": d["by"],
                                   "frozen_seq": d["frozen_seq"], "status": "ready"})
            if s["stage"] == "material":
                s["stage"] = "embroidery"
        elif t == "MaterialScrapped":
            for m in s["materials"]:
                if m["batch_id"] == d["batch_id"]:
                    m["status"] = "scrapped"
            s["materials"].append({"batch_id": d["batch_id"], "fabric": d.get("fabric", ""),
                                   "length_m": d.get("length_m", 0), "at": d["at"],
                                   "by": d["by"], "frozen_seq": d["frozen_seq"],
                                   "status": "scrapped", "reason": d["reason"]})
            # 尚未有刺绣完工时，报废批次意味着重回备料，等待替代批次检验
            embroidery_done = any(
                t["status"] == "completed" and
                CRAFT_STAGE.get(t["craft"]) == "embroidery"
                for t in s["tasks"])
            if s["stage"] == "embroidery" and not embroidery_done:
                s["stage"] = "material"

        elif t == "TaskAssigned":
            s["tasks"].append({
                "id": d["id"], "craft": d["craft"], "stage": d["stage"],
                "skill": d["skill"], "assignee": d["assignee"],
                "scope": d["scope"], "frozen_seq": d["frozen_seq"],
                "parent_task_id": d.get("parent_task_id"),
                "status": "assigned", "logged_minutes": 0, "logs": [],
                "assigned_at": d["at"],
            })
        elif t == "TaskStarted":
            task = _task(s, d["task_id"])
            task["status"] = "in_progress"
            task["started_at"] = d["at"]
        elif t == "WorkLogged":
            task = _task(s, d["task_id"])
            task["logged_minutes"] += d["minutes"]
            task["logs"].append({"at": d["at"], "minutes": d["minutes"], "note": d.get("note", "")})
        elif t == "TaskCompleted":
            task = _task(s, d["task_id"])
            task["status"] = "completed"
            task["completed_at"] = d["at"]
        elif t == "TaskRejected":
            task = _task(s, d["task_id"])
            task["status"] = "rejected"
            task["reject_reason"] = d["reason"]
            task["rejected_at"] = d["at"]

        elif t == "StageAdvanced":
            # 阶段在该阶段全部任务完成后由主管推进
            s["stage"] = d["to_stage"]

        elif t == "ChangeProposed":
            s["changes"].append({
                "id": d["id"], "kind": d["kind"], "reasons": d["reasons"],
                "fee_addition": d.get("fee_addition", 0), "currency": d.get("currency", "GBP"),
                "new_promised_date": d.get("new_promised_date"),
                "target_revision": d.get("target_revision"),
                "status": "proposed", "proposed_by": d["by"], "proposed_at": d["at"],
                "blocks_stages_from": d.get("blocks_stages_from"),
            })
        elif t == "ChangeReconfirmed":
            ch = _change(s, d["change_id"])
            ch["status"] = "reconfirmed"
            ch["reconfirmed_by"] = d["by"]
            ch["reconfirmed_at"] = d["at"]
            if d.get("new_frozen_seq"):
                s["confirmed"] = _find_frozen(s, d["new_frozen_seq"])
        elif t == "ChangeDeclined":
            ch = _change(s, d["change_id"])
            ch["status"] = "declined"
            ch["declined_by"] = d["by"]
            ch["declined_at"] = d["at"]

        elif t == "FittingScheduled":
            s["fittings"].append({"id": d["id"], "scheduled_at": d["scheduled_at"],
                                  "store_id": d["store_id"], "by": d["by"],
                                  "tz": d.get("tz"), "status": "scheduled"})
        elif t == "FittingDone":
            f = _fitting(s, d["fitting_id"])
            f["status"] = "done"
            f["actual_at"] = d["actual_at"]
            f["note"] = d.get("note", "")
        elif t == "FittingLate":
            f = _fitting(s, d["fitting_id"])
            f["status"] = "late"
            f["late_minutes"] = d["late_minutes"]

        elif t == "OrderDelivered":
            s["stage"] = "delivered"
            s["delivered"] = {
                "at": d["at"], "by": d["by"],
                "actual_date": d["actual_date"],
                "garment_record": d["garment_record"],
            }

    return s


def _task(s, task_id):
    for t in s["tasks"]:
        if t["id"] == task_id:
            return t
    raise NotFound(f"工序 {task_id} 不存在")


def _change(s, change_id):
    for c in s["changes"]:
        if c["id"] == change_id:
            return c
    raise NotFound(f"变更单 {change_id} 不存在")


def _fitting(s, fitting_id):
    for f in s["fittings"]:
        if f["id"] == fitting_id:
            return f
    raise NotFound(f"试衣 {fitting_id} 不存在")


def _find_frozen(s, seq):
    for f in reversed(s["frozen_history"]):
        if f["seq"] == seq:
            return f
    raise NotFound(f"制作版本 {seq} 不存在")


def _require_stage(s, allowed, action):
    if s["stage"] in _TERMINAL:
        raise Conflict(f"订单已{s['stage']}，不能再{action}")
    if s["stage"] not in allowed:
        raise Conflict(f"当前阶段为 {s['stage']}，不能{action}")


def _open_change(s):
    return next((c for c in s["changes"] if c["status"] == "proposed"), None)


class OrderAggregate:
    """每个方法返回新事件 data 列表；状态由 fold 得出，调用方负责持久化。"""

    def __init__(self, state):
        self.s = state

    # ---------- 咨询 ----------
    @staticmethod
    def open(customer_id, store_id, tz, actor, at):
        return [_ev("OrderOpened", customer_id=customer_id, store_id=store_id,
                    tz=tz, at=at, by=actor["id"])]

    def add_note(self, text, actor, at):
        return [_ev("NoteAdded", text=text, by=actor["id"], at=at)]

    # ---------- 故事与传播许可 ----------
    def record_story(self, text, cultural_meaning, actor, at):
        _require_stage(self.s, {"inquiry", "design", "material", "embroidery",
                                "cutting", "sewing", "fitting"}, "记录图案故事")
        if not text:
            raise ValidationFailed("故事内容不能为空")
        return [_ev("StoryRecorded", text=text, cultural_meaning=cultural_meaning,
                    by=actor["id"], at=at)]

    def grant_consent(self, scope, actor, at, valid_scopes):
        if scope not in valid_scopes:
            raise ValidationFailed(f"未知传播许可范围：{scope}")
        if self.s["grants"].get(scope):
            raise Conflict(f"传播许可 {scope} 已授予，无需重复授权")
        return [_ev("StoryConsentGranted", scope=scope, by=actor["id"], at=at)]

    def withdraw_consent(self, scope, actor, at):
        # 撤回传播许可永远合法（只要授过），且不影响制作合同
        if scope not in self.s["grants"]:
            raise Conflict(f"传播许可 {scope} 当前并未授予")
        return [_ev("StoryConsentWithdrawn", scope=scope, by=actor["id"], at=at)]

    # ---------- 设计 ----------
    def submit_design(self, revision, elements, embroidery_area, fabric_spec, notes,
                      actor, at, required_crafts=None):
        _require_stage(self.s, {"inquiry", "design"}, "提交稿版")
        if revision in self.s["designs"]:
            # 稿版号一旦存在即不可覆盖——防止偷偷替换
            raise Conflict(f"稿版 {revision} 已存在，稿版只能新增新版本，不能覆盖")
        if not elements:
            raise ValidationFailed("稿版必须包含至少一个关键元素")
        payload = dict(revision=revision, elements=elements,
                       embroidery_area=embroidery_area or {}, fabric_spec=fabric_spec or {},
                       notes=notes or "", required_crafts=required_crafts or [],
                       by=actor["id"], at=at)
        if self.s["stage"] == "inquiry":
            return [
                _ev("DesignSubmitted", **payload),
                _ev("StageAdvanced", to_stage="design", at=at, by=actor["id"]),
            ]
        return [_ev("DesignSubmitted", **payload)]

    def take_measurement(self, data, actor, at, new_date=None, fee_addition=0):
        """量体；确认后的复测（备料/刺绣阶段）自动产生待确认变更。

        裁剪一旦开始，尺寸冻结，不再接受复测——改尺寸只能走改稿变更单。
        """
        _require_stage(self.s, {"inquiry", "design", "material", "embroidery"}, "量体/复测")
        if not data:
            raise ValidationFailed("量体数据不能为空")
        version = len(self.s["measurement_versions"]) + 1
        events = [_ev("MeasurementTaken", version=version, data=data,
                      by=actor["id"], at=at)]
        if self.s["confirmed"] is not None:
            cid = f"chg-{len(self.s['changes']) + 1}"
            events.append(_ev(
                "ChangeProposed", id=cid, kind="measurement_change",
                reasons=[f"量体复测 v{version} 与确认版本 "
                         f"v{self.s['confirmed']['measurement_version']} 不同，需顾客再确认尺寸"],
                fee_addition=fee_addition or 0,
                currency=self.s["confirmed"]["quote"]["currency"],
                new_promised_date=new_date or self.s["confirmed"]["promised_date"],
                blocks_stages_from="cutting", by=actor["id"], at=at))
        return events

    def give_quote(self, amount, currency, breakdown, actor, at):
        _require_stage(self.s, {"inquiry", "design"}, "报价")
        if amount <= 0:
            raise ValidationFailed("报价金额必须大于零")
        return [_ev("QuoteGiven", amount=amount, currency=currency,
                    breakdown=breakdown or {}, by=actor["id"], at=at)]

    # ---------- 顾客确认：冻结制作版本 ----------
    def confirm(self, design_revision, elements, measurement_version, promised_date, actor, at):
        _require_stage(self.s, {"design"}, "确认订单")
        if design_revision not in self.s["designs"]:
            raise ValidationFailed(f"稿版 {design_revision} 不存在")
        mv = next((m for m in self.s["measurement_versions"]
                   if m["version"] == measurement_version), None)
        if mv is None:
            raise ValidationFailed(f"量体版本 {measurement_version} 不存在")
        if self.s["quote"] is None:
            raise Conflict("尚未报价，无法确认")
        design = self.s["designs"][design_revision]
        missing = [k for k in elements if k not in design["elements"]]
        if missing:
            raise ValidationFailed(f"确认的关键元素不在稿版中：{missing}")
        if not elements:
            raise ValidationFailed("必须确认至少一个关键元素")
        manifest = _manifest(design_revision, elements, measurement_version,
                             {"amount": self.s["quote"]["amount"],
                              "currency": self.s["quote"]["currency"]})
        seq = len(self.s["frozen_history"]) + 1
        return [_ev("OrderConfirmed", seq=seq, design_revision=design_revision,
                    elements=elements, measurement_version=measurement_version,
                    quote={"amount": self.s["quote"]["amount"],
                           "currency": self.s["quote"]["currency"],
                           "breakdown": self.s["quote"].get("breakdown", {})},
                    price=self.s["quote"]["amount"], promised_date=promised_date,
                    manifest=manifest, by=actor["id"], at=at)]

    def verify_manifest(self):
        """重算当前冻结版本哈希，供外部核查制作版本是否被替换。"""
        f = self.s["confirmed"]
        if not f:
            raise Conflict("订单尚未确认，无制作版本")
        recomputed = _manifest(f["design_revision"], f["elements"],
                               f["measurement_version"], f["quote"])
        return {"frozen_seq": f["seq"], "stored_manifest": f["manifest"],
                "recomputed_manifest": recomputed,
                "intact": recomputed == f["manifest"]}

    # ---------- 收款（只追加） ----------
    def record_payment(self, amount, currency, purpose, actor, at):
        if self.s["confirmed"] is None:
            raise Conflict("订单未确认不能收款")
        if amount <= 0:
            raise ValidationFailed("收款金额必须大于零")
        pid = f"pay-{len(self.s['payments']) + 1}"
        return [_ev("PaymentRecorded", id=pid, amount=amount, currency=currency,
                    purpose=purpose, by=actor["id"], at=at)]

    # ---------- 备料 ----------
    def prepare_material(self, batch_id, fabric, length_m, actor, at):
        _require_stage(self.s, {"material"}, "登记材料批次")
        if any(m["batch_id"] == batch_id for m in self.s["materials"]):
            raise Conflict(f"材料批次 {batch_id} 已登记")
        return [_ev("MaterialPrepared", batch_id=batch_id, fabric=fabric,
                    length_m=length_m, frozen_seq=self.s["confirmed"]["seq"],
                    by=actor["id"], at=at)]

    def scrap_material(self, batch_id, reason, fee_addition, new_date, actor, at):
        batch = next((m for m in self.s["materials"] if m["batch_id"] == batch_id), None)
        if batch is None:
            raise NotFound(f"材料批次 {batch_id} 不存在")
        if batch["status"] == "scrapped":
            raise Conflict("该批次已报废")
        cid = f"chg-{len(self.s['changes']) + 1}"
        return [
            _ev("MaterialScrapped", batch_id=batch_id, fabric=batch["fabric"],
                length_m=batch["length_m"], reason=reason,
                frozen_seq=self.s["confirmed"]["seq"], by=actor["id"], at=at),
            _ev("ChangeProposed", id=cid, kind="material_scrap",
                reasons=[f"材料批次 {batch_id}（{batch['fabric']}）报废：{reason}"],
                fee_addition=fee_addition, currency=self.s["confirmed"]["quote"]["currency"],
                new_promised_date=new_date, blocks_stages_from=self.s["stage"],
                by=actor["id"], at=at),
        ]

    # ---------- 派工 / 工时 / 返工 ----------
    def assign_task(self, craft, assignee_id, actor, at, directory,
                    parent_task_id=None, allow_past_stage=False):
        if self.s["confirmed"] is None:
            raise Conflict("订单未确认，不能派工")
        if craft not in CRAFT_STAGE:
            raise ValidationFailed(f"未知工序：{craft}")
        stage = CRAFT_STAGE[craft]
        if not allow_past_stage and STAGES.index(stage) < STAGES.index(self.s["stage"]):
            raise Conflict(f"{stage} 阶段已经过，不能补派该工序")
        # 返工（关联原工序）是质量闭环，不受待确认变更单的派工冻结限制
        if not parent_task_id:
            ch = _open_change(self.s)
            if ch and STAGES.index(stage) >= STAGES.index(ch["blocks_stages_from"]):
                raise Conflict(f"存在待顾客再确认的变更单 {ch['id']}，该工序暂不能派工")
        assignee = directory.actor(assignee_id)
        if assignee["role"] != "artisan":
            raise ValidationFailed("任务只能派给绣娘/裁缝")
        skill = {"material": "material", "cut": "tailor", "tailor": "tailor"}.get(craft, craft)
        if skill not in assignee["skills"]:
            raise Conflict(
                f"{assignee['name']} 技能为 {assignee['skills']}，不具备 {skill} 技能，不能派工"
            )
        tid = f"task-{len(self.s['tasks']) + 1}"
        return [_ev("TaskAssigned", id=tid, craft=craft, stage=stage, skill=skill,
                    assignee=assignee_id, scope=TASK_SCOPE[craft],
                    frozen_seq=self.s["confirmed"]["seq"],
                    parent_task_id=parent_task_id, by=actor["id"], at=at)]

    def _own_task(self, task_id, actor):
        task = _task(self.s, task_id)
        if task["assignee"] != actor["id"]:
            raise Conflict("只能操作分配给自己的工序")
        return task

    def _assert_task_not_frozen(self, task):
        """待再确认变更会冻结其 blocks_stages_from 及之后阶段的一切工序推进。"""
        ch = _open_change(self.s)
        if ch and STAGES.index(task["stage"]) >= STAGES.index(ch["blocks_stages_from"]):
            raise Conflict(
                f"变更单 {ch['id']} 待顾客再确认（{ch['reasons'][0]}），"
                f"{task['stage']} 阶段工序已冻结，再确认前不能继续"
            )

    def start_task(self, task_id, actor, at):
        task = self._own_task(task_id, actor)
        if task["status"] != "assigned":
            raise Conflict(f"工序状态为 {task['status']}，不能开始")
        self._assert_task_not_frozen(task)
        return [_ev("TaskStarted", task_id=task_id, by=actor["id"], at=at)]

    def log_work(self, task_id, minutes, note, actor, at):
        task = self._own_task(task_id, actor)
        if task["status"] != "in_progress":
            raise Conflict("只有进行中的工序可以记录工时")
        self._assert_task_not_frozen(task)
        if minutes <= 0:
            raise ValidationFailed("工时分钟数必须大于零")
        return [_ev("WorkLogged", task_id=task_id, minutes=minutes,
                    note=note or "", by=actor["id"], at=at)]

    def complete_task(self, task_id, actor, at):
        task = self._own_task(task_id, actor)
        if task["status"] != "in_progress":
            raise Conflict(f"工序状态为 {task['status']}，不能完成")
        self._assert_task_not_frozen(task)
        return [_ev("TaskCompleted", task_id=task_id, by=actor["id"], at=at)]

    def reject_task(self, task_id, reason, rework_craft, actor, at, directory):
        """质检退回：原工序标记 rejected，并派一条关联原工序的返工任务。"""
        task = _task(self.s, task_id)
        if task["status"] != "completed":
            raise Conflict("只能退回已完成待验收的工序")
        events = [_ev("TaskRejected", task_id=task_id, reason=reason,
                      by=actor["id"], at=at)]
        # 返工以新工序记录关联原步骤（README）
        rework = self.assign_task(rework_craft or task["craft"], task["assignee"],
                                  actor, at, directory, parent_task_id=task_id,
                                  allow_past_stage=True)
        events.extend(rework)
        return events

    @staticmethod
    def _task_settled(s, task):
        """工序已收口：完成，或被退回且其关联返工已完成。"""
        if task["status"] == "completed":
            return True
        if task["status"] == "rejected":
            return any(
                c["parent_task_id"] == task["id"] and c["status"] == "completed"
                for c in s["tasks"]
            )
        return False

    def advance_stage(self, to_stage, actor, at):
        cur = STAGES.index(self.s["stage"])
        target = STAGES.index(to_stage)
        if target != cur + 1:
            raise Conflict("只能顺序推进到下一阶段")
        if _open_change(self.s):
            raise Conflict("存在待再确认的变更单，不能推进阶段")
        cur_seq = self.s["confirmed"]["seq"]
        redo_idx = STAGES.index(self.s["confirmed"].get("redo_from", "material"))
        # 当前阶段若早于重做起点，旧版本工序/批次继续有效；之后必须按新版本重做
        need_current = cur >= redo_idx
        design = self.s["designs"][self.s["confirmed"]["design_revision"]]
        planned = [c for c in design.get("required_crafts", [])
                   if CRAFT_STAGE.get(c) == self.s["stage"]]
        if self.s["stage"] == "material":
            ready = [m for m in self.s["materials"]
                     if m["status"] == "ready"
                     and (m.get("frozen_seq") == cur_seq if need_current else True)]
            if not ready:
                raise Conflict("当前版本尚无检验合格的材料批次，不能进入刺绣")
        else:
            for craft in planned:
                settled = [t for t in self.s["tasks"]
                           if t["craft"] == craft
                           and (t.get("frozen_seq") == cur_seq if need_current else True)
                           and self._task_settled(self.s, t)]
                if not settled:
                    raise Conflict(f"当前版本要求的 {craft} 工序尚未完成，不能进入 {to_stage}")
        pending = [t for t in self.s["tasks"]
                   if STAGES.index(t["stage"]) == cur
                   and (t.get("frozen_seq") == cur_seq if need_current else True)
                   and not self._task_settled(self.s, t)]
        if pending:
            raise Conflict(f"当前阶段还有 {len(pending)} 道工序未完成，不能进入 {to_stage}")
        return [_ev("StageAdvanced", to_stage=to_stage, by=actor["id"], at=at)]

    # ---------- 变更：改稿 ----------
    def request_design_change(self, new_revision, elements, embroidery_area, fabric_spec,
                              notes, fee_addition, new_date, actor, at, required_crafts=None):
        """顾客改稿：提交新稿版 + 变更单（新费用、新交期待再确认）。"""
        _require_stage(self.s, {"material", "embroidery", "cutting", "sewing", "fitting"},
                       "提出改稿")
        if new_revision in self.s["designs"]:
            raise Conflict(f"稿版 {new_revision} 已存在")
        if _open_change(self.s):
            raise Conflict("已有待再确认的变更单，请先处理")
        submit = _ev("DesignSubmitted", revision=new_revision, elements=elements,
                     embroidery_area=embroidery_area or {}, fabric_spec=fabric_spec or {},
                     notes=notes or "", required_crafts=required_crafts or [],
                     by=actor["id"], at=at)
        cid = f"chg-{len(self.s['changes']) + 1}"
        change = _ev("ChangeProposed", id=cid, kind="design_change",
                     reasons=[f"顾客改稿：{notes or '关键元素调整'}（新稿版 r{new_revision}）"],
                     fee_addition=fee_addition,
                     currency=self.s["confirmed"]["quote"]["currency"],
                     new_promised_date=new_date,
                     target_revision=new_revision,
                     blocks_stages_from=self.s["stage"], by=actor["id"], at=at)
        return [submit, change]

    def reconfirm(self, change_id, measurement_version, actor, at):
        """顾客再次确认变更：冻结新制作版本。

        无论哪类变更都生成新冻结版本（价格叠加附加费、交期更新为变更单日期）；
        原收款与已完成工时不动，新版本通过 frozen_seq 指向链接续。
        """
        ch = _change(self.s, change_id)
        if ch["status"] != "proposed":
            raise Conflict(f"变更单状态为 {ch['status']}，无需再确认")
        f = self.s["confirmed"]
        design_revision = f["design_revision"]
        elements = f["elements"]
        mv = measurement_version or f["measurement_version"]
        if ch["kind"] == "design_change":
            design_revision = ch["target_revision"]
            elements = self.s["designs"][design_revision]["elements"]
        elif ch["kind"] == "measurement_change":
            if not any(m["version"] == mv for m in self.s["measurement_versions"]):
                raise ValidationFailed(f"量体版本 {mv} 不存在")
        new_price = f["price"] + ch["fee_addition"]
        new_date = ch["new_promised_date"] or f["promised_date"]
        manifest = _manifest(design_revision, elements, mv,
                             {"amount": new_price, "currency": ch["currency"]})
        new_seq = len(self.s["frozen_history"]) + 1

        # 新版本要求重做的最早阶段；该阶段之前的旧工序/批次沿用
        redo_from = "material"
        if ch["kind"] == "design_change":
            new_design = self.s["designs"][design_revision]
            old_design = self.s["designs"][f["design_revision"]]
            new_fab = new_design.get("fabric_spec") or {}
            old_fab = old_design.get("fabric_spec") or {}
            # 新稿版未另给面料规格即视为沿用原面料
            if new_fab and new_fab != old_fab:
                redo_from = "material"
            elif any(c in new_design.get("required_crafts", [])
                     for c in ("suxiu", "xiangxiu", "beadwork")):
                redo_from = "embroidery"
            elif "cut" in new_design.get("required_crafts", []):
                redo_from = "cutting"
            else:
                redo_from = "sewing"
        elif ch["kind"] == "measurement_change":
            # 刺绣照旧，只需自裁剪起重做
            redo_from = "cutting"
        elif ch["kind"] == "material_scrap":
            redo_from = "material"
        elif ch["kind"] == "fitting_late":
            redo_from = "fitting"

        confirm = _ev("OrderConfirmed", seq=new_seq, design_revision=design_revision,
                      elements=elements, measurement_version=mv,
                      quote={**f["quote"], "amount": new_price, "currency": ch["currency"]},
                      price=new_price, promised_date=new_date,
                      manifest=manifest, redo_from=redo_from,
                      by=actor["id"], at=at)
        # 仅当重做起点早于当前阶段时，阶段才回退；否则（如复测只影响后续裁剪）原地不动
        if STAGES.index(redo_from) < STAGES.index(self.s["stage"]):
            confirm["data"]["reset_stage"] = redo_from
        return [
            confirm,
            _ev("ChangeReconfirmed", change_id=change_id,
                new_frozen_seq=new_seq, by=actor["id"], at=at),
        ]

    def decline_change(self, change_id, actor, at):
        ch = _change(self.s, change_id)
        if ch["status"] != "proposed":
            raise Conflict("只能拒绝待确认的变更单")
        return [_ev("ChangeDeclined", change_id=change_id, by=actor["id"], at=at)]

    # ---------- 试衣 ----------
    def schedule_fitting(self, scheduled_at, store_id, actor, at):
        _require_stage(self.s, {"sewing", "fitting"}, "安排试衣")
        if self.s["stage"] == "fitting":
            pass
        fid = f"fit-{len(self.s['fittings']) + 1}"
        events = [_ev("FittingScheduled", id=fid, scheduled_at=scheduled_at,
                      store_id=store_id, tz=self.s["tz"], by=actor["id"], at=at)]
        if self.s["stage"] == "sewing":
            events.append(_ev("StageAdvanced", to_stage="fitting", by=actor["id"], at=at))
        return events

    def mark_fitting_done(self, fitting_id, actual_at, note, actor, at):
        f = _fitting(self.s, fitting_id)
        if f["status"] not in ("scheduled", "late"):
            raise Conflict(f"试衣状态为 {f['status']}")
        return [_ev("FittingDone", fitting_id=fitting_id, actual_at=actual_at,
                    note=note or "", by=actor["id"], at=at)]

    def mark_fitting_late(self, fitting_id, late_minutes, fee_addition, new_date, actor, at):
        """顾客迟到：超过等待窗口记迟到，产生费用与交期变更待再确认。"""
        f = _fitting(self.s, fitting_id)
        if f["status"] != "scheduled":
            raise Conflict("只能将已预约试衣标记为迟到")
        cid = f"chg-{len(self.s['changes']) + 1}"
        return [
            _ev("FittingLate", fitting_id=fitting_id, late_minutes=late_minutes,
                by=actor["id"], at=at),
            _ev("ChangeProposed", id=cid, kind="fitting_late",
                reasons=[f"顾客试衣迟到 {late_minutes} 分钟，门店与裁缝档期需重排"],
                fee_addition=fee_addition,
                currency=self.s["confirmed"]["quote"]["currency"],
                new_promised_date=new_date, blocks_stages_from="fitting",
                by=actor["id"], at=at),
        ]

    # ---------- 交付与成衣记录 ----------
    def deliver(self, actor, at):
        _require_stage(self.s, {"fitting"}, "交付")
        if not any(f["status"] == "done" for f in self.s["fittings"]):
            raise Conflict("尚无完成的试衣记录，不能交付")
        if _open_change(self.s):
            raise Conflict("存在待再确认的变更单，不能交付")
        f = self.s["confirmed"]
        actual_date = parse_iso(at).date().isoformat()
        delayed = actual_date > f["promised_date"]
        delay_reasons = [r for c in self.s["changes"]
                         if c["status"] == "reconfirmed" for r in c["reasons"]]
        record = {
            "order_store": self.s["store_id"],
            "frozen_seq": f["seq"],
            "manifest": f["manifest"],
            "promised_date": f["promised_date"],
            "actual_date": actual_date,
            "delivered_by": actor["id"],
            "delivered_at": at,
            "delayed": delayed,
            "delay_reasons": delay_reasons if delayed else [],
            # 确认人链：最初确认 + 每次变更再确认
            "confirmer_chain": [
                {"seq": h["seq"], "by": h["by"], "at": h["at"],
                 "promised_date": h["promised_date"]}
                for h in self.s["frozen_history"]
            ],
            "total_paid": sum(p["amount"] for p in self.s["payments"]),
            "final_price": f["price"],
            "logged_minutes": sum(t["logged_minutes"] for t in self.s["tasks"]),
            # 成衣对外讲述时，只能使用当前仍获许可的故事范围
            "story": None if not self.s["story"] else {
                "recorded_by": self.s["story"]["recorded_by"],
                "cultural_meaning": self.s["story"]["cultural_meaning"],
                "public_scopes": sorted(self.s["grants"].keys()),
                "withdrawn_scopes": sorted(self.s["withdrawn_scopes"].keys()),
                "text": self.s["story"]["text"] if self.s["grants"] else None,
            },
        }
        return [_ev("OrderDelivered", actual_date=actual_date,
                    garment_record=record, by=actor["id"], at=at)]
