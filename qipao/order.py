"""订单聚合：咨询到交付的全部业务规则与不变量。

设计要点：
- 每个命令追加不可变事件，version 随事件递增（乐观锁）。
- 故事"制作授权"与"传播许可"分离：制作授权绑定已确认版本，不可撤回；
  传播许可按范围授予、可随时撤回，撤回以事件留痕、立即对所有读模型生效。
- 顾客确认后形成带指纹的制作版本；任何关键元素/尺寸/报价/面料变化
  必须走变更单并由顾客再次确认费用与日期，旧版本永久留档、不得偷换。
- 费用与工时为只增账本：收款、返工工时永不删除，更正只能追加对冲条目。
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from .catalog import (
    EMBROIDERY_STYLES,
    PROCESS_BY_STAGE,
    PROCESS_TEMPLATE,
    STAGE_ROLES,
    person,
    store,
)
from .clock import SystemClock, format_instant, parse_instant
from .errors import ConflictError, NotFoundError, ValidationError


class Phase(str, Enum):
    CONSULTATION = "consultation"
    DESIGN = "design"
    AWAITING_APPROVAL = "awaiting_approval"
    MATERIAL = "material_prep"
    PRODUCTION = "production"
    FITTING = "fitting"
    DELIVERED = "delivered"


PUBLICATION_SCOPES = (
    "workshop_internal",  # 工坊内部制作人员可见故事
    "store_marketing",    # 门店社交平台营销
    "public_anonymous",   # 公开但匿名
)

# 需在派工系统中分配工匠的工序；其余里程碑由领域动作自动物化
MANUAL_STAGES = ("embroidery", "cutting", "tailoring")


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 实体快照
# ---------------------------------------------------------------------------

@dataclass
class MeasurementSet:
    version: int
    points: dict
    taken_by: str
    taken_at: str
    note: str = ""
    remeasure_reason: str = ""


@dataclass
class DesignDraft:
    id: str
    seq: int
    submitted_by: str
    submitted_at: str
    elements: list           # 关键元素：纹样/盘扣/领型/袖长等
    embroidery_style: str    # suzhou / hunan / none
    inspiration: str
    revision_of: str | None = None


@dataclass
class ApprovedVersion:
    version_no: int
    fingerprint: str
    draft_id: str
    measurement_version: int
    elements: list
    embroidery_style: str
    fabric_batch_id: str
    quote: dict
    currency: str
    approved_by: str
    approved_at: str                 # UTC
    approved_local: str              # 顾客所在时区本地时间
    customer_zone: str
    store_id: str
    change_order_id: str | None = None
    supersedes: str | None = None


@dataclass
class PublicationGrant:
    scope: str
    granted: bool
    at: str
    by: str
    reason: str = ""


@dataclass
class ChangeOrder:
    id: str
    reason: str                      # customer_redesign / late_customer / material_scrap / other
    summary: str
    affected_stage: str
    extra_work_days: float
    cost_delta: float
    currency: str
    proposed_by: str
    proposed_at: str
    status: str = "proposed"         # proposed / confirmed / rejected / cancelled
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    new_version_no: int | None = None
    changes: dict = field(default_factory=dict)


@dataclass
class LedgerEntry:
    seq: int
    kind: str                        # charge / payment / refund / adjustment / work_log
    amount: float
    currency: str
    at: str
    by: str
    ref: dict = field(default_factory=dict)
    note: str = ""
    hours: float | None = None
    artisan_id: str | None = None
    stage: str | None = None


@dataclass
class Task:
    id: str
    stage: str
    attempt: int
    assignee: str | None
    status: str = "assigned"         # assigned / started / done / rework_required
    assigned_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    rework_of: str | None = None
    rework_reason: str = ""
    ordered_by: str | None = None


@dataclass
class MaterialAllocation:
    batch_id: str
    meters: float
    at: str
    by: str


@dataclass
class Fitting:
    id: str
    scheduled_at: str
    store_id: str
    status: str                     # scheduled / done / no_show
    result: str = ""                # ok / alterations
    notes: str = ""
    recorded_by: str | None = None
    recorded_at: str | None = None


@dataclass
class Delay:
    reason: str
    days: float
    source: str                     # change_order / rework
    ref: str
    confirmed_by: str
    confirmed_at: str
    detail: str = ""


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

class Order:
    def __init__(self, order_id, customer_id, store_id, created_by, at=None,
                 clock=None):
        self._clock = clock or SystemClock()
        self.id = order_id
        self.customer_id = customer_id
        self.store_ids = [store_id]          # 两家门店可共同服务同一订单
        self.owner_store_id = store_id
        self.version = 0
        self.phase = Phase.CONSULTATION.value
        self.events = []
        self.created_by = created_by
        self.created_at = format_instant(at or self._clock.now())

        self.story = None                   # {content, significance, recorded_by, at}
        self.recording_consent = None       # 顾客授权记录故事
        self.production_grant = None        # 制作授权（绑定首个确认版本，不可撤）
        self.publication = {}               # scope -> [PublicationGrant...]（追加）

        self.measurements = []              # MeasurementSet
        self.drafts = []                    # DesignDraft
        self.approved_versions = []         # ApprovedVersion（全部历史版本）
        self.change_orders = []
        self.ledger = []
        self.tasks = []
        self.allocations = []
        self.scrapped_batches = []          # [{batch_id, at, by, reason}]
        self.fittings = []
        self.delays = []
        self.delivery = None

    # -- 事件与版本 ---------------------------------------------------------

    def _append(self, etype, actor, data):
        self.version += 1
        event = {
            "seq": self.version,
            "at": format_instant(self._clock.now()),
            "type": etype,
            "actor": actor,
            "data": data,
        }
        self.events.append(event)
        return event

    @staticmethod
    def _require_role(actor, roles):
        p = person(actor) if isinstance(actor, str) else actor
        if p is None:
            raise ValidationError(f"未知操作人员：{actor}")
        if p.role not in roles:
            raise ValidationError(
                f"角色 {p.role} 不能执行该操作，需要 {'/'.join(roles)}"
            )
        return p

    # -- 门店协作 -----------------------------------------------------------

    def add_store(self, actor, store_id):
        """第二家门店加入同一订单（两店共改一单的前提）。"""
        self._require_role(actor, ("store_staff", "manager"))
        if store(store_id) is None:
            raise NotFoundError(f"门店不存在：{store_id}")
        if store_id in self.store_ids:
            raise ConflictError("该门店已在订单中")
        self.store_ids.append(store_id)
        self._append("store_added", actor if isinstance(actor, str) else actor.id,
                     {"store_id": store_id})

    # -- 顾客故事与授权 -----------------------------------------------------

    def record_story(self, actor, customer, content, significance):
        self._require_role(actor, ("store_staff", "designer", "manager"))
        cust = person(customer) if isinstance(customer, str) else customer
        if cust is None or cust.role != "customer":
            raise ValidationError("故事必须属于顾客")
        if cust.id != self.customer_id:
            raise ValidationError("只能记录本订单顾客的故事")
        if self.story is not None:
            raise ConflictError("故事已记录，请使用更新接口并保留历史")
        self.story = {
            "content": content,
            "significance": significance,
            "recorded_by": actor if isinstance(actor, str) else actor.id,
            "at": format_instant(self._clock.now()),
        }
        self._append("story_recorded", self.story["recorded_by"],
                     {"customer": cust.id, "significance": significance})

    def consent_recording(self, actor, granted, reason=""):
        """顾客授权/撤回"记录"本身。记录授权可撤回，但已留档的制作授权不受影响。"""
        p = self._require_role(actor, ("customer",))
        if p.id != self.customer_id:
            raise ValidationError("只有顾客本人能管理故事授权")
        self.recording_consent = {
            "granted": granted, "at": format_instant(self._clock.now()), "reason": reason,
        }
        self._append("recording_consent", p.id,
                     {"granted": granted, "reason": reason})

    def set_publication(self, actor, scope, granted, reason=""):
        """授予或撤回某一传播范围。撤回立即生效且对成衣记录同样收窄。"""
        p = self._require_role(actor, ("customer",))
        if p.id != self.customer_id:
            raise ValidationError("只有顾客本人能管理传播许可")
        if scope not in PUBLICATION_SCOPES:
            raise ValidationError(f"未知传播范围：{scope}")
        grant = PublicationGrant(scope, granted, format_instant(self._clock.now()), p.id, reason)
        self.publication.setdefault(scope, []).append(grant)
        self._append("publication_changed", p.id,
                     {"scope": scope, "granted": granted, "reason": reason})

    def publication_active(self, scope):
        """某传播范围当前是否有效（最后一次授权事件为准）。"""
        history = self.publication.get(scope)
        return bool(history and history[-1].granted)

    # -- 量体（复测产生新版本，旧版留档） -----------------------------------

    def add_measurement(self, actor, points, note="", remeasure_reason=""):
        p = self._require_role(actor, ("store_staff", "tailor", "manager"))
        required = {"chest", "waist", "hip", "length", "shoulder"}
        missing = required - set(points or {})
        if missing:
            raise ValidationError(f"量体数据缺少：{sorted(missing)}")
        version_no = len(self.measurements) + 1
        ms = MeasurementSet(
            version=version_no, points=dict(points),
            taken_by=p.id, taken_at=format_instant(self._clock.now()),
            note=note, remeasure_reason=remeasure_reason,
        )
        self.measurements.append(ms)
        self._materialize_milestone("consultation", p.id)
        if self.phase == Phase.CONSULTATION.value:
            self.phase = Phase.DESIGN.value
        self._append("measurement_recorded", p.id, {
            "version": version_no, "remeasure_reason": remeasure_reason,
        })
        return version_no

    # -- 设计稿（可多版） ---------------------------------------------------

    def submit_draft(self, actor, elements, embroidery_style, inspiration="",
                     revision_of=None):
        p = self._require_role(actor, ("designer",))
        if not elements:
            raise ValidationError("设计稿必须包含至少一个关键元素")
        if embroidery_style not in ("none", *EMBROIDERY_STYLES.keys()):
            raise ValidationError(f"未知绣种：{embroidery_style}")
        if revision_of and not any(d.id == revision_of for d in self.drafts):
            raise NotFoundError(f"原稿件不存在：{revision_of}")
        seq = len(self.drafts) + 1
        draft = DesignDraft(
            id=f"{self.id}_draft_{seq}", seq=seq, submitted_by=p.id,
            submitted_at=format_instant(self._clock.now()), elements=list(elements),
            embroidery_style=embroidery_style, inspiration=inspiration,
            revision_of=revision_of,
        )
        self.drafts.append(draft)
        if self.phase == Phase.DESIGN.value:
            self.phase = Phase.AWAITING_APPROVAL.value
        self._append("draft_submitted", p.id, {
            "draft_id": draft.id, "seq": seq, "revision_of": revision_of,
        })
        return draft.id

    # -- 顾客确认：锁定不可替换的制作版本 -----------------------------------

    @property
    def current_version(self):
        return self.approved_versions[-1] if self.approved_versions else None

    def _quote_total(self, quote):
        return round(sum(float(line["amount"]) for line in quote["lines"]), 2)

    def approve_version(self, actor, draft_id, measurement_version, quote,
                        fabric_batch_id, currency="GBP"):
        """顾客确认关键元素、尺寸与报价，形成带指纹的制作版本。"""
        p = self._require_role(actor, ("customer",))
        if p.id != self.customer_id:
            raise ValidationError("只有顾客本人能确认制作版本")
        if self.story and self.recording_consent and not self.recording_consent["granted"]:
            raise ConflictError("故事记录授权已被撤回，不能进入制作确认")
        draft = next((d for d in self.drafts if d.id == draft_id), None)
        if draft is None:
            raise NotFoundError(f"稿件不存在：{draft_id}")
        ms = next((m for m in self.measurements if m.version == measurement_version), None)
        if ms is None:
            raise NotFoundError(f"量体版本不存在：{measurement_version}")
        if not quote or not quote.get("lines"):
            raise ValidationError("报价不能为空")
        for line in quote["lines"]:
            if "amount" not in line or float(line["amount"]) < 0:
                raise ValidationError("报价行必须含非负金额")
        if not fabric_batch_id:
            raise ValidationError("必须指定面料批次")

        version_no = len(self.approved_versions) + 1
        basis = {
            "draft_id": draft.id,
            "elements": draft.elements,
            "embroidery_style": draft.embroidery_style,
            "measurement_version": ms.version,
            "measurement_points": ms.points,
            "quote_lines": quote["lines"],
            "fabric_batch_id": fabric_batch_id,
        }
        fp = fingerprint(basis)
        cust_zone = _zone_of(self.customer_id)
        inst = self._clock.now()
        approved = ApprovedVersion(
            version_no=version_no, fingerprint=fp, draft_id=draft.id,
            measurement_version=ms.version, elements=list(draft.elements),
            embroidery_style=draft.embroidery_style,
            fabric_batch_id=fabric_batch_id, quote=quote, currency=currency,
            approved_by=p.id, approved_at=format_instant(inst),
            approved_local=format_instant(inst, cust_zone),
            customer_zone=cust_zone, store_id=self.owner_store_id,
        )
        self.approved_versions.append(approved)
        self._materialize_milestone("design", p.id)
        if self.production_grant is None:
            # 制作授权随首次确认成立：已确认的制作合同不因传播撤回而解除
            self.production_grant = {
                "granted_by": p.id, "version_no": version_no,
                "at": approved.approved_at,
            }
            self._append("production_granted", p.id, {"version_no": version_no})
        if self.phase in (Phase.AWAITING_APPROVAL.value, Phase.DESIGN.value):
            self.phase = Phase.MATERIAL.value
        # 报价同时形成应收
        total = self._quote_total(quote)
        self._ledger("charge", total, currency, p.id,
                     ref={"version_no": version_no}, note="确认报价")
        self._append("version_approved", p.id, {
            "version_no": version_no, "fingerprint": fp,
            "draft_id": draft.id, "measurement_version": ms.version,
            "fabric_batch_id": fabric_batch_id, "total": total,
        })
        return approved

    def verify_production_basis(self, *, fabric_batch_id=None, elements=None,
                                measurement_version=None, draft_id=None):
        """校验实际用料/做法仍等于当前锁定版本，防止偷换。

        任何差异必须先经顾客确认的变更单产生新版本。
        """
        cur = self.current_version
        if cur is None:
            raise ConflictError("尚无顾客确认的制作版本")
        if fabric_batch_id is not None and fabric_batch_id != cur.fabric_batch_id:
            raise ConflictError(
                "面料批次与确认版本不一致：替换面料必须经变更单再次确认",
                details={"locked": cur.fabric_batch_id, "submitted": fabric_batch_id},
            )
        if elements is not None and list(elements) != cur.elements:
            raise ConflictError("关键元素与确认版本不一致：改稿必须经变更单再次确认")
        if measurement_version is not None and measurement_version != cur.measurement_version:
            raise ConflictError("尺寸版本与确认版本不一致：复测后必须再次确认")
        if draft_id is not None and draft_id != cur.draft_id:
            raise ConflictError("稿件与确认版本不一致")
        return True

    # -- 变更单：改稿/迟到/材料报废 → 费用与日期再确认 ----------------------

    def raise_change_order(self, actor, reason, summary, affected_stage,
                           cost_delta, extra_work_days, changes=None, currency="GBP"):
        p = self._require_role(actor, ("store_staff", "designer", "manager"))
        if self.current_version is None:
            raise ConflictError("尚未确认制作版本，直接修改即可，无需变更单")
        if affected_stage not in PROCESS_BY_STAGE:
            raise ValidationError(f"未知工序：{affected_stage}")
        if reason not in ("customer_redesign", "late_customer", "material_scrap", "other"):
            raise ValidationError(f"未知变更原因：{reason}")
        co = ChangeOrder(
            id=f"{self.id}_co_{len(self.change_orders) + 1}",
            reason=reason, summary=summary, affected_stage=affected_stage,
            extra_work_days=float(extra_work_days), cost_delta=float(cost_delta),
            currency=currency, proposed_by=p.id, proposed_at=format_instant(self._clock.now()),
            changes=changes or {},
        )
        self.change_orders.append(co)
        self._append("change_proposed", p.id, {
            "change_id": co.id, "reason": reason,
            "cost_delta": co.cost_delta, "extra_work_days": co.extra_work_days,
            "affected_stage": affected_stage,
        })
        return co.id

    def amend_change_order(self, actor, change_id, changes, cost_delta=None):
        """变更单待确认期间，门店补录具体方案（如新面料批次）与费用。

        顾客尚未确认，方案可以修正；一旦确认即锁定，只能再开新变更单。
        """
        p = self._require_role(actor, ("store_staff", "manager"))
        co = self._get_change(change_id)
        if co.status != "proposed":
            raise ConflictError(f"变更单状态为 {co.status}，不能修改")
        co.changes.update(changes)
        if cost_delta is not None:
            co.cost_delta = float(cost_delta)
        self._append("change_amended", p.id,
                     {"change_id": co.id, "changes": changes,
                      "cost_delta": co.cost_delta})

    def confirm_change_order(self, actor, change_id):
        """顾客确认新费用与日期；确认后生成新版本指纹，旧版本完整保留。"""
        p = self._require_role(actor, ("customer",))
        if p.id != self.customer_id:
            raise ValidationError("只有顾客本人能确认变更")
        co = self._get_change(change_id)
        if co.status != "proposed":
            raise ConflictError(f"变更单状态为 {co.status}，不能确认")
        if co.reason == "material_scrap" and not co.changes.get("fabric_batch_id"):
            raise ConflictError("面料报废变更必须先补录替代批次，才能请顾客确认")
        cur = self.current_version
        new_basis = {
            "draft_id": cur.draft_id,
            "elements": co.changes.get("elements", cur.elements),
            "embroidery_style": co.changes.get("embroidery_style", cur.embroidery_style),
            "measurement_version": co.changes.get(
                "measurement_version", cur.measurement_version),
            "measurement_points": self._measurement_points(
                co.changes.get("measurement_version", cur.measurement_version)),
            "quote_lines": cur.quote["lines"],
            "fabric_batch_id": co.changes.get("fabric_batch_id", cur.fabric_batch_id),
            "change_order_id": co.id,
        }
        fp = fingerprint(new_basis)
        new_version = ApprovedVersion(
            version_no=cur.version_no + 1, fingerprint=fp,
            draft_id=cur.draft_id,
            measurement_version=new_basis["measurement_version"],
            elements=list(new_basis["elements"]),
            embroidery_style=new_basis["embroidery_style"],
            fabric_batch_id=new_basis["fabric_batch_id"],
            quote=cur.quote, currency=cur.currency,
            approved_by=p.id, approved_at=format_instant(self._clock.now()),
            approved_local=format_instant(self._clock.now(), cur.customer_zone),
            customer_zone=cur.customer_zone, store_id=cur.store_id,
            change_order_id=co.id, supersedes=cur.fingerprint,
        )
        self.approved_versions.append(new_version)
        co.status = "confirmed"
        co.confirmed_by = p.id
        co.confirmed_at = format_instant(self._clock.now())
        co.new_version_no = new_version.version_no
        if co.cost_delta:
            self._ledger(
                "adjustment", co.cost_delta, co.currency, p.id,
                ref={"change_order_id": co.id},
                note=f"变更补费：{co.summary}",
            )
        self.delays.append(Delay(
            reason=co.reason, days=co.extra_work_days, source="change_order",
            ref=co.id, confirmed_by=p.id, confirmed_at=co.confirmed_at,
            detail=co.summary,
        ))
        # 迟到改约确认后生成新的试衣预约
        rescheduled = co.changes.get("rescheduled_fitting_at")
        if rescheduled:
            old = next((f for f in self.fittings if f.status == "no_show"), None)
            self.fittings.append(Fitting(
                id=f"{self.id}_fit_{len(self.fittings) + 1}",
                scheduled_at=rescheduled,
                store_id=old.store_id if old else self.owner_store_id,
                status="scheduled",
            ))
        self._append("change_confirmed", p.id, {
            "change_id": co.id, "new_version_no": new_version.version_no,
            "new_fingerprint": fp, "cost_delta": co.cost_delta,
            "extra_work_days": co.extra_work_days,
        })
        return new_version

    def reject_change_order(self, actor, change_id):
        p = self._require_role(actor, ("customer",))
        if p.id != self.customer_id:
            raise ValidationError("只有顾客本人能拒绝变更")
        co = self._get_change(change_id)
        if co.status != "proposed":
            raise ConflictError(f"变更单状态为 {co.status}，不能拒绝")
        co.status = "rejected"
        self._append("change_rejected", p.id, {"change_id": co.id})

    def has_open_change(self):
        return any(co.status == "proposed" for co in self.change_orders)

    def _get_change(self, change_id):
        co = next((c for c in self.change_orders if c.id == change_id), None)
        if co is None:
            raise NotFoundError(f"变更单不存在：{change_id}")
        return co

    def _measurement_points(self, version):
        ms = next((m for m in self.measurements if m.version == version), None)
        return ms.points if ms else None

    # -- 面料：批次锁定、领料校验、报废 -------------------------------------

    def allocate_material(self, actor, batch_id, meters, registry_batches):
        """备料领料：必须与当前确认版本的面料批次一致，且批次未报废。"""
        p = self._require_role(actor, ("manager", "store_staff"))
        if self.phase not in (Phase.MATERIAL.value, Phase.PRODUCTION.value):
            raise ConflictError(f"当前阶段 {self.phase} 不能备料")
        self.verify_production_basis(fabric_batch_id=batch_id)
        batch = registry_batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"面料批次不存在：{batch_id}")
        if batch.get("scrapped") or any(b["batch_id"] == batch_id for b in self.scrapped_batches):
            raise ConflictError("面料批次已报废，不能领用")
        used = sum(a.meters for a in self.allocations if a.batch_id == batch_id)
        if used + float(meters) > float(batch["meters"]):
            raise ConflictError(
                f"面料库存不足：批次余料 {batch['meters'] - used} 米，申请 {meters} 米")
        alloc = MaterialAllocation(batch_id, float(meters), format_instant(self._clock.now()), p.id)
        self.allocations.append(alloc)
        self._materialize_milestone("material_prep", p.id)
        self.phase = Phase.PRODUCTION.value
        self._append("material_allocated", p.id,
                     {"batch_id": batch_id, "meters": meters})

    def scrap_material(self, actor, batch_id, reason, registry_batches):
        """材料报废：批次台账置废、订单留痕，并挂起变更单
        （新批次/费用/日期需门店补录、顾客再确认）。"""
        p = self._require_role(actor, ("manager", "store_staff"))
        if not any(a.batch_id == batch_id for a in self.allocations):
            raise ConflictError("该订单未领用此批次，无需在此报废")
        if any(b["batch_id"] == batch_id for b in self.scrapped_batches):
            raise ConflictError("该批次已登记报废")
        self.scrapped_batches.append(
            {"batch_id": batch_id, "at": format_instant(self._clock.now()),
             "by": p.id, "reason": reason})
        batch = registry_batches.get(batch_id)
        if batch is not None:
            batch["scrapped"] = True
            batch["scrap_reason"] = reason
        self._append("material_scrapped", p.id,
                     {"batch_id": batch_id, "reason": reason})
        return self.raise_change_order(
            p.id, reason="material_scrap",
            summary=f"面料报废：{reason}",
            affected_stage="material_prep",
            cost_delta=0, extra_work_days=2.0,
            changes={"fabric_batch_id": None},  # 门店补录新批次后再确认
        )
        return self.raise_change_order(
            p.id, reason="material_scrap",
            summary=f"面料报废：{reason}",
            affected_stage="material_prep",
            cost_delta=0, extra_work_days=2.0,
            changes={"fabric_batch_id": None},  # 门店补录新批次后再确认
        )

    # -- 派工与工序（含返工） -----------------------------------------------

    def _materialize_milestone(self, stage, actor_id):
        """把领域动作（量体/确认/领料）物化为工艺路线上的完工工序。

        consultation/design/material_prep 三道里程碑不由派工系统分配，
        但它们是后续工序的依赖，需要完工记录与时间点供排程和追溯使用。
        """
        if any(t.stage == stage and t.status == "done" for t in self.tasks):
            return
        at = format_instant(self._clock.now())
        attempt = len([t for t in self.tasks if t.stage == stage]) + 1
        self.tasks.append(Task(
            id=f"{self.id}_{stage}_{attempt}", stage=stage, attempt=attempt,
            assignee=actor_id, status="done",
            assigned_at=at, started_at=at, finished_at=at,
        ))

    def assign_task(self, actor, stage, assignee):
        """派工：校验技能角色、未完成依赖、存在未确认变更时不得开工。"""
        p = self._require_role(actor, ("manager", "store_staff"))
        if stage not in MANUAL_STAGES:
            raise ValidationError(
                f"{PROCESS_BY_STAGE[stage]['name'] if stage in PROCESS_BY_STAGE else stage}"
                "由业务动作驱动（量体/确认/领料/试衣/交付），不能手工派工")
        artisan = person(assignee)
        if artisan is None or artisan.role not in STAGE_ROLES[stage]:
            raise ValidationError(
                f"{assignee} 的角色不能承担 {PROCESS_BY_STAGE[stage]['name']}")
        if self.has_open_change():
            raise ConflictError("存在待顾客确认的变更单，不得派工")
        self._assert_stage_unlocked(stage)
        if stage in ("material_prep", "embroidery", "cutting", "tailoring"):
            if self.current_version is None:
                raise ConflictError("制作版本尚未确认")
        self._assert_dependencies_done(stage)
        existing = [t for t in self.tasks if t.stage == stage and t.assignee == assignee
                    and t.status in ("assigned", "started")]
        if existing:
            raise ConflictError("该工匠已有同一工序的在制任务")
        attempt = len([t for t in self.tasks if t.stage == stage]) + 1
        task = Task(
            id=f"{self.id}_{stage}_{attempt}", stage=stage, attempt=attempt,
            assignee=artisan.id, assigned_at=format_instant(self._clock.now()),
        )
        self.tasks.append(task)
        self._append("task_assigned", p.id,
                     {"task_id": task.id, "stage": stage, "assignee": artisan.id})
        return task.id

    def start_task(self, actor, task_id):
        p = self._require_role(actor, ("embroiderer", "tailor", "designer",
                                       "store_staff", "manager"))
        task = self._get_task(task_id)
        if task.assignee != p.id:
            raise ValidationError("只能开始派给自己的任务")
        if task.status != "assigned":
            raise ConflictError(f"任务状态为 {task.status}")
        self._assert_stage_unlocked(task.stage)
        task.status = "started"
        task.started_at = format_instant(self._clock.now())
        self._append("task_started", p.id, {"task_id": task_id})

    def complete_task(self, actor, task_id):
        p = self._require_role(actor, ("embroiderer", "tailor", "designer",
                                       "store_staff", "manager"))
        task = self._get_task(task_id)
        if task.assignee != p.id:
            raise ValidationError("只能完成派给自己的任务")
        if task.status != "started":
            raise ConflictError(f"任务状态为 {task.status}，不能完工")
        task.status = "done"
        task.finished_at = format_instant(self._clock.now())
        self._append("task_completed", p.id, {"task_id": task_id})
        self._advance_phase()

    def request_rework(self, actor, task_id, reason):
        """主管/门店要求返工：原任务标记，生成关联的新任务（attempt+1）。"""
        p = self._require_role(actor, ("manager", "store_staff"))
        task = self._get_task(task_id)
        if task.status != "done":
            raise ConflictError("只能对已完工工序要求返工")
        task.status = "rework_required"
        task.rework_reason = reason
        attempt = len([t for t in self.tasks if t.stage == task.stage]) + 1
        rework = Task(
            id=f"{self.id}_{task.stage}_{attempt}", stage=task.stage, attempt=attempt,
            assignee=task.assignee, assigned_at=format_instant(self._clock.now()),
            rework_of=task.id, rework_reason=reason, ordered_by=p.id,
        )
        self.tasks.append(rework)
        self.delays.append(Delay(
            reason="rework", days=0.0, source="rework", ref=rework.id,
            confirmed_by=p.id, confirmed_at=format_instant(self._clock.now()),
            detail=reason,
        ))
        self._append("rework_requested", p.id,
                     {"task_id": task_id, "rework_id": rework.id, "reason": reason})
        return rework.id

    def _get_task(self, task_id):
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return task

    def _assert_dependencies_done(self, stage):
        for dep in PROCESS_BY_STAGE[stage]["deps"]:
            done = [t for t in self.tasks if t.stage == dep and t.status == "done"]
            if not done:
                raise ConflictError(
                    f"前置工序 {PROCESS_BY_STAGE[dep]['name']} 尚未完工，不能派工 {stage}")

    def _assert_stage_unlocked(self, stage):
        """若待确认变更影响该工序本身或其上游，则该工序锁定；并行工序不受影响。"""
        for co in self.change_orders:
            if co.status != "proposed":
                continue
            if co.affected_stage == stage or stage in _downstream(co.affected_stage):
                raise ConflictError(
                    f"工序 {stage} 受待确认变更单 {co.id} 影响，暂停")

    def _advance_phase(self):
        done_stages = {t.stage for t in self.tasks if t.status == "done"}
        if "delivery" in done_stages:
            self.phase = Phase.DELIVERED.value
        elif "fitting" in done_stages:
            self.phase = Phase.DELIVERED.value if self.delivery else Phase.FITTING.value
        elif "tailoring" in done_stages:
            self.phase = Phase.FITTING.value

    # -- 工时与费用（只增账本） ---------------------------------------------

    def log_work(self, actor, task_id, hours, hourly_rate, note=""):
        """工匠登记工时；工时与对应工费立即固化，返工/改稿不冲销旧工时。"""
        p = self._require_role(actor, ("embroiderer", "tailor", "designer",
                                       "store_staff", "manager"))
        task = self._get_task(task_id)
        if task.assignee != p.id:
            raise ValidationError("只能登记自己任务的工时")
        if float(hours) <= 0:
            raise ValidationError("工时必须为正")
        amount = round(float(hours) * float(hourly_rate), 2)
        self._ledger("work_log", amount, self.current_version.currency
                     if self.current_version else "GBP",
                     p.id, ref={"task_id": task_id},
                     note=note, hours=float(hours), artisan_id=p.id, stage=task.stage)

    def _ledger(self, kind, amount, currency, by, ref=None, note="", **extra):
        entry = LedgerEntry(
            seq=len(self.ledger) + 1, kind=kind, amount=round(float(amount), 2),
            currency=currency, at=format_instant(self._clock.now()), by=by,
            ref=ref or {}, note=note, **extra,
        )
        self.ledger.append(entry)
        self._append(f"ledger_{kind}", by,
                     {"seq": entry.seq, "amount": entry.amount, "currency": currency,
                      **({"hours": entry.hours} if entry.hours is not None else {}),
                      "ref": entry.ref, "note": note})
        return entry.seq

    def record_payment(self, actor, amount, currency="GBP", note=""):
        """门店登记顾客付款（实收，永不因后续变更被删除或改写）。"""
        p = self._require_role(actor, ("store_staff", "manager"))
        if float(amount) <= 0:
            raise ValidationError("收款金额必须为正")
        return self._ledger("payment", amount, currency, p.id, note=note)

    def ledger_totals(self):
        totals = {"charges": 0.0, "payments": 0.0, "work_cost": 0.0,
                  "work_hours": 0.0, "currency": "GBP"}
        for e in self.ledger:
            totals["currency"] = e.currency
            if e.kind in ("charge", "adjustment"):
                totals["charges"] += e.amount
            elif e.kind == "payment":
                totals["payments"] += e.amount
            elif e.kind == "work_log":
                totals["work_cost"] += e.amount
                totals["work_hours"] += e.hours or 0
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in totals.items()}

    # -- 试衣（迟到/缺席也推动状态） ----------------------------------------

    def schedule_fitting(self, actor, when, store_id=None):
        p = self._require_role(actor, ("store_staff", "manager"))
        if self.phase != Phase.FITTING.value and \
                not any(t.stage == "tailoring" and t.status == "done" for t in self.tasks):
            raise ConflictError("缝制完工后才能预约试衣")
        sid = store_id or self.owner_store_id
        if sid not in self.store_ids:
            raise NotFoundError(f"门店未加入本订单：{sid}")
        fitting = Fitting(
            id=f"{self.id}_fit_{len(self.fittings) + 1}",
            scheduled_at=format_instant(parse_instant(when)),
            store_id=sid, status="scheduled",
        )
        self.fittings.append(fitting)
        self.phase = Phase.FITTING.value
        self._append("fitting_scheduled", p.id,
                     {"fitting_id": fitting.id, "at": fitting.scheduled_at,
                      "store_id": sid})
        return fitting.id

    def mark_customer_late(self, actor, fitting_id, reschedule_at, penalty=0.0,
                           extra_work_days=0.0):
        """顾客迟到/缺席：新日期与费用进入变更单，顾客再次确认后生效。"""
        p = self._require_role(actor, ("store_staff", "manager"))
        fitting = self._get_fitting(fitting_id)
        fitting.status = "no_show"
        self._append("fitting_no_show", p.id, {"fitting_id": fitting_id})
        return self.raise_change_order(
            p.id, reason="late_customer",
            summary=f"顾客缺席试衣 {fitting_id}，改约",
            affected_stage="fitting", cost_delta=penalty,
            extra_work_days=extra_work_days,
            changes={"rescheduled_fitting_at": format_instant(parse_instant(reschedule_at))},
        )

    def record_fitting(self, actor, fitting_id, result, notes=""):
        p = self._require_role(actor, ("store_staff", "manager"))
        fitting = self._get_fitting(fitting_id)
        if fitting.status == "done":
            raise ConflictError("试衣已记录")
        if result not in ("ok", "alterations"):
            raise ValidationError("试衣结果必须为 ok 或 alterations")
        fitting.status = "done"
        fitting.result = result
        fitting.notes = notes
        fitting.recorded_by = p.id
        fitting.recorded_at = format_instant(self._clock.now())
        self._append("fitting_recorded", p.id,
                     {"fitting_id": fitting_id, "result": result})
        if result == "ok":
            self._materialize_milestone("fitting", p.id)
            self._advance_phase()
        if result == "alterations":
            tailoring_done = [t for t in self.tasks
                              if t.stage == "tailoring" and t.status == "done"]
            if tailoring_done:
                return self.request_rework(p.id, tailoring_done[-1].id,
                                           f"试衣修改：{notes}")

    def _get_fitting(self, fitting_id):
        fitting = next((f for f in self.fittings if f.id == fitting_id), None)
        if fitting is None:
            raise NotFoundError(f"试衣不存在：{fitting_id}")
        return fitting

    # -- 交付 ---------------------------------------------------------------

    def deliver(self, actor):
        p = self._require_role(actor, ("store_staff", "manager"))
        fit = next((f for f in self.fittings if f.status == "done" and f.result == "ok"), None)
        if fit is None:
            raise ConflictError("必须有一次结果为 ok 的试衣才能交付")
        if self.has_open_change():
            raise ConflictError("仍有待确认变更单，不能交付")
        if self.current_version is None:
            raise ConflictError("无确认版本")
        self.phase = Phase.DELIVERED.value
        self.delivery = {"by": p.id, "at": format_instant(self._clock.now()),
                         "fitting_id": fit.id, "store_id": fit.store_id}
        self._materialize_milestone("delivery", p.id)
        self._append("delivered", p.id, self.delivery)
        return self.delivery

    # -- 快照 ---------------------------------------------------------------

    def to_dict(self):
        return {
            "id": self.id,
            "version": self.version,
            "phase": self.phase,
            "customer_id": self.customer_id,
            "stores": self.store_ids,
            "approved_version_no": self.current_version.version_no
            if self.current_version else None,
            "fingerprint": self.current_version.fingerprint if self.current_version else None,
            "events": self.events,
        }


def _zone_of(customer_id):
    from .catalog import REGISTRY
    cust = REGISTRY["people"].get(customer_id)
    sid = cust.store_id if cust else None
    return REGISTRY["stores"][sid]["zone"] if sid and sid in REGISTRY["stores"] else "UTC"


def _downstream(stage):
    """返回受某工序变更影响的全部下游工序。"""
    result = set()
    stack = [stage]
    while stack:
        cur = stack.pop()
        for item in PROCESS_TEMPLATE:
            if cur in item["deps"] and item["stage"] not in result:
                result.add(item["stage"])
                stack.append(item["stage"])
    return result
