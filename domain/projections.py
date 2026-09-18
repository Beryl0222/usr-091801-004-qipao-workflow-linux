"""读模型投影：从订单状态派生各类角色视图。

最小信息原则：绣娘/裁缝只能看到自己任务所需字段（TASK_SCOPE），
不接触顾客身份故事全文、报价与其他工序。
"""

from .timeutil import local_str

STAGE_LABELS = {
    "inquiry": "咨询",
    "design": "设计确认",
    "material": "备料",
    "embroidery": "刺绣",
    "cutting": "裁剪",
    "sewing": "缝制",
    "fitting": "试衣",
    "delivered": "交付",
}


def _with_local_time(frozen, tz_name):
    """在冻结版本上附加顾客所在时区的确认时刻（不改动领域状态）。"""
    if not frozen:
        return frozen
    out = dict(frozen)
    if tz_name:
        out["confirmed_at_local"] = local_str(frozen["at"], tz_name)
    return out


def order_view(state, directory):
    """门店/管理视图：完整订单，但故事文本按当前许可过滤。"""
    store = directory.stores.get(state["store_id"], {})
    return {
        "stage": state["stage"],
        "stage_label": STAGE_LABELS.get(state["stage"]),
        "customer_id": state["customer_id"],
        "store_id": state["store_id"],
        "store_name": store.get("name"),
        "customer_timezone": state["tz"],
        "opened_at": state["opened_at"],
        "story": _story_view(state),
        "designs": list(state["designs"].values()),
        "measurement_versions": [
            {"version": m["version"], "at": m["at"], "by": m["by"]}
            for m in state["measurement_versions"]
        ],
        "quote": state["quote"],
        "confirmed": _with_local_time(state["confirmed"], state.get("tz")),
        "frozen_history": [_with_local_time(f, state.get("tz")) for f in state["frozen_history"]],
        "payments": state["payments"],
        "paid_total": sum(p["amount"] for p in state["payments"]),
        "balance": (state["confirmed"]["price"] - sum(p["amount"] for p in state["payments"]))
        if state["confirmed"] else None,
        "materials": state["materials"],
        "tasks": state["tasks"],
        "changes": state["changes"],
        "fittings": state["fittings"],
        "notes": state["notes"],
        "delivered": state["delivered"],
    }


def _story_view(state):
    """故事的传播视图：无任何许可时不返回正文，只保留文化含义说明与授权状态。"""
    story = state["story"]
    if story is None:
        return None
    return {
        "cultural_meaning": story["cultural_meaning"],
        "recorded_at": story["recorded_at"],
        "recorded_by": story["recorded_by"],
        "granted_scopes": sorted(state["grants"].keys()),
        "withdrawn_scopes": sorted(state["withdrawn_scopes"].keys()),
        # 仅当至少有一项传播许可生效时，外部视图才含故事正文
        "text": story["text"] if state["grants"] else None,
    }


def artisan_view(state, directory, actor):
    """绣娘/裁缝的最小信息视图：只含本人任务与任务 scope 内的制作数据。"""
    frozen = state["confirmed"]
    design = state["designs"][frozen["design_revision"]] if frozen else None
    measure = next((m for m in state["measurement_versions"]
                    if m["version"] == frozen["measurement_version"]), None) if frozen else None
    tasks = []
    for t in state["tasks"]:
        if t["assignee"] != actor["id"]:
            continue
        info = {"task_id": t["id"],
                "craft": t["craft"], "stage": t["stage"], "status": t["status"],
                "logged_minutes": t["logged_minutes"],
                "frozen_manifest": t["frozen_seq"],
                "parent_task_id": t.get("parent_task_id")}
        # 严格按任务 scope 暴露字段
        if "fabric_spec" in t["scope"]:
            info["fabric_spec"] = design["fabric_spec"] if design else None
        if "motif_elements" in t["scope"]:
            info["motif_elements"] = design["elements"] if design else None
            info["embroidery_area"] = design["embroidery_area"] if design else None
        if "pattern_elements" in t["scope"]:
            info["pattern_elements"] = design["elements"] if design else None
        if "measurements" in t["scope"]:
            info["measurements"] = measure["data"] if measure else None
        tasks.append(info)
    return {
        "artisan": actor["id"],
        "name": actor["name"],
        "skills": actor["skills"],
        "my_tasks": tasks,
        # 不返回：顾客身份/故事/报价/其他工匠任务
    }


def garment_record_view(state):
    """成衣记录（交付后）：延期原因、确认人链、获准公开的故事范围。"""
    if state["delivered"] is None:
        return None
    return state["delivered"]["garment_record"]


def store_board(states_by_id, directory, store_id=None):
    """门店看板：阶段、冻结版本、待确认变更、交期风险摘要。"""
    rows = []
    for oid, state in states_by_id.items():
        if store_id and state["store_id"] != store_id:
            continue
        pending = next((c for c in state["changes"] if c["status"] == "proposed"), None)
        rows.append({
            "order_id": oid,
            "stage": state["stage"],
            "stage_label": STAGE_LABELS.get(state["stage"]),
            "customer_id": state["customer_id"],
            "store_id": state["store_id"],
            "frozen_seq": state["confirmed"]["seq"] if state["confirmed"] else None,
            "promised_date": state["confirmed"]["promised_date"] if state["confirmed"] else None,
            "pending_change": pending["id"] if pending else None,
            "open_tasks": sum(1 for t in state["tasks"] if t["status"] in
                              ("assigned", "in_progress", "rejected")),
            "delivered": state["stage"] == "delivered",
        })
    return rows
