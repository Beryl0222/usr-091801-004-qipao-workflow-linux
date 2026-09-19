"""读模型投影：按角色裁剪可见信息，绣娘与裁缝只拿到完成任务所需的最小字段。

- 绣娘：自己的刺绣任务 + 锁定纹样元素/绣种/面料；看不到顾客身份、故事全文、
  尺寸、报价与其他工序。故事文化含义仅在顾客授予 workshop_internal 时
  以"工艺提示"形式给出。
- 裁缝：自己的裁剪/缝制任务 + 锁定量体尺寸/影响剪裁的元素/面料；
  看不到报价、故事与顾客身份。
- 门店/设计/主管：完整业务视图。
- 成衣记录：交付后对外可追溯的证据链——确认人、延期原因、获准公开的故事范围；
  传播许可一旦撤回，记录中的故事内容立即被遮蔽，只保留撤回留痕。
"""

from .catalog import EMBROIDERY_STYLES, REGISTRY, person


def _person_label(pid):
    p = REGISTRY["people"].get(pid)
    return {"id": pid, "name": p.name, "role": p.role} if p else {"id": pid}


# -- 工匠最小信息 -----------------------------------------------------------

# 绣娘需要看到的关键元素类别；其余元素（如与绣工无关的里料偏好）不下发
EMBROIDERY_ELEMENT_WHITELIST = {"motif", "pattern_story", "thread_palette",
                                "cuff", "collar", "lapel"}
# 裁缝剪裁需要的元素
TAILORING_ELEMENT_WHITELIST = {"silhouette", "length", "sleeve", "collar",
                               "fastener", "hem", "lining"}


def _filter_elements(elements, whitelist):
    out = []
    for el in elements or []:
        kind = el.get("kind") if isinstance(el, dict) else None
        if kind in whitelist:
            out.append(el)
    return out


def artisan_view(order, artisan_id):
    """绣娘/裁缝的任务卡：只有自己的在制与待开始任务及工艺必需信息。"""
    p = person(artisan_id)
    if p is None:
        return {"artisan": {"id": artisan_id}, "tasks": []}

    mine = [t for t in order.tasks
            if t.assignee == artisan_id and t.status in ("assigned", "started")]
    cards = []
    cur = order.current_version
    for t in mine:
        card = {
            "task_id": t.id,
            "stage": t.stage,
            "attempt": t.attempt,
            "status": t.status,
            "rework_of": t.rework_of,
            "rework_reason": t.rework_reason,
        }
        if cur is not None:
            card["locked_fingerprint"] = cur.fingerprint
            card["fabric_batch_id"] = cur.fabric_batch_id
            if p.role == "embroiderer" and t.stage == "embroidery":
                style = EMBROIDERY_STYLES.get(cur.embroidery_style)
                card["embroidery_style"] = cur.embroidery_style
                card["embroidery_style_name"] = style["name"] if style else None
                card["elements"] = _filter_elements(
                    cur.elements, EMBROIDERY_ELEMENT_WHITELIST)
                # 故事的文化含义：仅在内部传播许可有效时给出，且只给工艺提示
                if order.story and order.publication_active("workshop_internal"):
                    card["craft_note"] = order.story.get("significance")
            elif p.role == "tailor":
                ms = next((m for m in order.measurements
                           if m.version == cur.measurement_version), None)
                card["measurement_version"] = cur.measurement_version
                card["measurements"] = ms.points if ms else None
                card["elements"] = _filter_elements(
                    cur.elements, TAILORING_ELEMENT_WHITELIST)
        cards.append(card)

    return {
        "artisan": {"id": p.id, "name": p.name, "role": p.role},
        "order_ref": order.id,   # 工单号，不含顾客信息
        "tasks": cards,
    }


# -- 门店完整视图 -----------------------------------------------------------

def staff_view(order, actor):
    p = person(actor)
    is_manager = p is not None and p.role == "manager"
    ledger = None
    if is_manager or (p and p.role == "store_staff"):
        totals = order.ledger_totals()
        ledger = {
            "entries": [
                {
                    "seq": e.seq, "kind": e.kind, "amount": e.amount,
                    "currency": e.currency, "at": e.at, "by": e.by,
                    "hours": e.hours, "stage": e.stage, "note": e.note,
                    "ref": e.ref,
                }
                for e in order.ledger
            ],
            "totals": totals,
            "balance_due": round(totals["charges"] - totals["payments"], 2),
        }
    return {
        "order_id": order.id,
        "version": order.version,
        "phase": order.phase,
        "stores": order.store_ids,
        "customer": _person_label(order.customer_id),
        "story": _story_view(order),
        "measurements": [
            {"version": m.version, "points": m.points, "taken_by": m.taken_by,
             "taken_at": m.taken_at, "note": m.note,
             "remeasure_reason": m.remeasure_reason}
            for m in order.measurements
        ],
        "drafts": [
            {"id": d.id, "seq": d.seq, "submitted_by": d.submitted_by,
             "submitted_at": d.submitted_at, "elements": d.elements,
             "embroidery_style": d.embroidery_style, "revision_of": d.revision_of}
            for d in order.drafts
        ],
        "approved_versions": [_version_view(v, order) for v in order.approved_versions],
        "change_orders": [
            {
                "id": c.id, "reason": c.reason, "summary": c.summary,
                "affected_stage": c.affected_stage, "cost_delta": c.cost_delta,
                "currency": c.currency, "extra_work_days": c.extra_work_days,
                "status": c.status, "proposed_by": c.proposed_by,
                "proposed_at": c.proposed_at, "confirmed_by": c.confirmed_by,
                "confirmed_at": c.confirmed_at, "new_version_no": c.new_version_no,
            }
            for c in order.change_orders
        ],
        "tasks": [
            {"id": t.id, "stage": t.stage, "attempt": t.attempt,
             "assignee": t.assignee, "status": t.status,
             "assigned_at": t.assigned_at, "started_at": t.started_at,
             "finished_at": t.finished_at, "rework_of": t.rework_of,
             "rework_reason": t.rework_reason, "ordered_by": t.ordered_by}
            for t in order.tasks
        ],
        "materials": {
            "allocations": [vars(a) for a in order.allocations],
            "scrapped": order.scrapped_batches,
        },
        "fittings": [vars(f) for f in order.fittings],
        "delivery": order.delivery,
        "ledger": ledger,
    }


def _version_view(v, order):
    return {
        "version_no": v.version_no,
        "fingerprint": v.fingerprint,
        "draft_id": v.draft_id,
        "measurement_version": v.measurement_version,
        "elements": v.elements,
        "embroidery_style": v.embroidery_style,
        "fabric_batch_id": v.fabric_batch_id,
        "quote": v.quote,
        "currency": v.currency,
        "approved_by": v.approved_by,
        "approved_at": v.approved_at,
        "approved_local": v.approved_local,
        "customer_zone": v.customer_zone,
        "change_order_id": v.change_order_id,
        "supersedes": v.supersedes,
    }


def _story_view(order):
    if order.story is None:
        return None
    scopes = {}
    for scope, history in order.publication.items():
        latest = history[-1]
        scopes[scope] = {
            "active": latest.granted,
            "last_changed_at": latest.at,
            "last_changed_by": latest.by,
            "reason": latest.reason,
        }
    return {
        "content": order.story["content"],
        "significance": order.story["significance"],
        "recorded_by": order.story["recorded_by"],
        "recorded_at": order.story.get("at"),
        "recording_consent": order.recording_consent,
        "scopes": scopes,
    }


# -- 对外公开故事视图（撤回即遮蔽） -----------------------------------------


def public_story_view(order):
    """社交平台/公开渠道可取用的故事内容。

    - store_marketing 有效：可署名展示；
    - 仅 public_anonymous 有效：匿名展示；
    - 均无效（含撤回）：content 一律返回 None，并显式说明不可公开。
    """
    if order.story is None:
        return {"publishable": False, "reason": "no_story"}
    marketing = order.publication_active("store_marketing")
    anonymous = order.publication_active("public_anonymous")
    history = []
    for scope in ("store_marketing", "public_anonymous"):
        for g in order.publication.get(scope, []):
            history.append({"scope": scope, "granted": g.granted, "at": g.at,
                            "by": g.by, "reason": g.reason})
    history.sort(key=lambda g: g["at"])
    if not marketing and not anonymous:
        last = history[-1] if history else None
        return {
            "publishable": False,
            "reason": "revoked" if last and not last["granted"] else "never_granted",
            "last_decision": last,
            "history": history,
            "content": None,
        }
    return {
        "publishable": True,
        "attribution": "named" if marketing else "anonymous",
        "customer_label": _person_label(order.customer_id) if marketing else None,
        "content": order.story["content"],
        "significance": order.story["significance"],
        "history": history,
    }


# -- 成衣记录（验收证据链） -------------------------------------------------

def garment_record(order):
    """交付成衣的可追溯记录：版本指纹、确认人、延期归因、可公开故事范围。"""
    delivered = order.delivery is not None
    current = order.current_version
    delays = []
    for d in order.delays:
        delays.append({
            "reason": d.reason,
            "reason_text": _DELAY_REASON_TEXT.get(d.reason, d.reason),
            "days": d.days,
            "source": d.source,
            "ref": d.ref,
            "detail": d.detail,
            "confirmed_by": _person_label(d.confirmed_by),
            "confirmed_at": d.confirmed_at,
        })
    record = {
        "order_id": order.id,
        "status": "delivered" if delivered else order.phase,
        "garment_version": _version_view(current, order) if current else None,
        "version_history": [
            {"version_no": v.version_no, "fingerprint": v.fingerprint,
             "approved_by": _person_label(v.approved_by),
             "approved_at_utc": v.approved_at,
             "approved_at_customer_local": v.approved_local,
             "customer_zone": v.customer_zone,
             "change_order_id": v.change_order_id,
             "supersedes": v.supersedes}
            for v in order.approved_versions
        ],
        "delay_explanation": delays,
        "total_confirmed_delay_days": round(sum(d["days"] for d in delays), 2),
        "reworks": [
            {"task_id": t.id, "stage": t.stage, "rework_of": t.rework_of,
             "reason": t.rework_reason, "ordered_by": _person_label(t.ordered_by),
             "assigned_at": t.assigned_at}
            for t in order.tasks if t.rework_of is not None
        ],
        "material": {
            "final_batch_id": current.fabric_batch_id if current else None,
            "scrapped": order.scrapped_batches,
        },
        "fittings": [
            {"id": f.id, "scheduled_at": f.scheduled_at, "store_id": f.store_id,
             "status": f.status, "result": f.result, "notes": f.notes,
             "recorded_by": f.recorded_by, "recorded_at": f.recorded_at}
            for f in order.fittings
        ],
        "delivery": order.delivery,
        "public_story": public_story_view(order),
    }
    return record


_DELAY_REASON_TEXT = {
    "customer_redesign": "顾客改稿",
    "late_customer": "顾客迟到或缺席试衣",
    "material_scrap": "真丝面料批次报废",
    "rework": "工序返工",
}
