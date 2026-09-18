"""应用服务：鉴权 + 命令分发 + 乐观并发，串起目录、仓储、调度与投影。

HTTP 层与测试都只依赖这一层；时钟可注入（跨时区/节假日演练用）。
"""

from .directory import CONSENT_SCOPES
from .errors import PermissionDenied, ValidationFailed
from .order import OrderAggregate
from .repository import OrderRepository
from .scheduler import Scheduler
from .projections import artisan_view, garment_record_view, order_view, store_board
from .timeutil import now_utc, to_iso

# 命令 -> 允许角色
ROLE_PERMISSIONS = {
    "add_note": {"store", "designer", "manager"},
    "record_story": {"customer", "store"},
    "grant_consent": {"customer"},
    "withdraw_consent": {"customer"},
    "submit_design": {"designer"},
    "take_measurement": {"store", "manager"},
    "give_quote": {"store", "manager"},
    "confirm": {"customer"},
    "record_payment": {"store", "customer", "manager"},
    "prepare_material": {"manager"},
    "scrap_material": {"manager"},
    "assign_task": {"manager"},
    "start_task": {"artisan"},
    "log_work": {"artisan"},
    "complete_task": {"artisan"},
    "reject_task": {"manager"},
    "advance_stage": {"manager"},
    "request_design_change": {"customer", "store"},
    "reconfirm": {"customer"},
    "decline_change": {"customer"},
    "schedule_fitting": {"store", "manager"},
    "mark_fitting_done": {"store", "manager"},
    "mark_fitting_late": {"store", "manager"},
    "deliver": {"manager", "store"},
}


class Application:
    def __init__(self, directory, repository=None, clock=None):
        self.dir = directory
        self.repo = repository or OrderRepository()
        self.scheduler = Scheduler(directory)
        self.clock = clock or now_utc

    def _actor(self, actor_id):
        if not actor_id:
            raise PermissionDenied("缺少操作人（X-Actor）")
        return self.dir.actor(actor_id)

    def _time(self):
        return to_iso(self.clock())

    # ---- 建单 ----
    def create_order(self, order_id, payload, actor_id):
        actor = self._actor(actor_id)
        self.dir.require_role(actor_id, {"customer", "store"})
        store_id = payload.get("store_id") or actor.get("store_id")
        if store_id not in self.dir.stores:
            raise ValidationFailed("必须指定有效门店 store_id")
        customer_id = payload["customer_id"]
        if actor["role"] == "customer" and actor["id"] != customer_id:
            raise PermissionDenied("顾客只能为自己建单")
        customer = self.dir.actor(customer_id)
        if customer["role"] != "customer":
            raise ValidationFailed(f"{customer_id} 不是顾客账号")
        events = OrderAggregate.open(customer_id, store_id,
                                     payload.get("tz") or customer.get("tz"),
                                     actor, self._time())
        self.repo.create(order_id, events)
        return self.view(order_id, actor_id)

    # ---- 命令 ----
    def command(self, order_id, cmd, payload, actor_id, expected_version=None):
        actor = self._actor(actor_id)
        allowed = ROLE_PERMISSIONS.get(cmd)
        if allowed is None:
            raise ValidationFailed(f"未知命令：{cmd}")
        self.dir.require_role(actor_id, allowed)
        at = self._time()

        def decision(agg):
            s = agg.s
            # 顾客只能操作自己的订单
            if actor["role"] == "customer" and s["customer_id"] != actor["id"]:
                raise PermissionDenied("不能操作其他顾客的订单")
            return self._dispatch(agg, cmd, payload, actor, at)

        result = self.repo.mutate(order_id, decision, expected_version)
        return {"version": result["version"], "applied": [e["type"] for e in result["events"]]}

    def _dispatch(self, agg, cmd, p, actor, at):
        d = self.dir
        if cmd == "add_note":
            return agg.add_note(p["text"], actor, at)
        if cmd == "record_story":
            return agg.record_story(p["text"], p.get("cultural_meaning", ""), actor, at)
        if cmd == "grant_consent":
            return agg.grant_consent(p["scope"], actor, at, set(CONSENT_SCOPES))
        if cmd == "withdraw_consent":
            return agg.withdraw_consent(p["scope"], actor, at)
        if cmd == "submit_design":
            return agg.submit_design(
                p["revision"], p["elements"], p.get("embroidery_area"),
                p.get("fabric_spec"), p.get("notes"), actor, at,
                required_crafts=p.get("required_crafts"))
        if cmd == "take_measurement":
            return agg.take_measurement(p["data"], actor, at,
                                        new_date=p.get("new_promised_date"),
                                        fee_addition=p.get("fee_addition", 0))
        if cmd == "give_quote":
            return agg.give_quote(p["amount"], p.get("currency", "GBP"),
                                  p.get("breakdown"), actor, at)
        if cmd == "confirm":
            return agg.confirm(p["design_revision"], p["elements"],
                               p["measurement_version"], p["promised_date"], actor, at)
        if cmd == "record_payment":
            return agg.record_payment(p["amount"], p.get("currency", "GBP"),
                                      p.get("purpose", "payment"), actor, at)
        if cmd == "prepare_material":
            return agg.prepare_material(p["batch_id"], p["fabric"], p["length_m"], actor, at)
        if cmd == "scrap_material":
            return agg.scrap_material(p["batch_id"], p["reason"],
                                      p.get("fee_addition", 0), p["new_promised_date"],
                                      actor, at)
        if cmd == "assign_task":
            return agg.assign_task(p["craft"], p["assignee"], actor, at, d,
                                   parent_task_id=p.get("parent_task_id"),
                                   allow_past_stage=bool(p.get("allow_past_stage")))
        if cmd == "start_task":
            return agg.start_task(p["task_id"], actor, at)
        if cmd == "log_work":
            return agg.log_work(p["task_id"], p["minutes"], p.get("note"), actor, at)
        if cmd == "complete_task":
            return agg.complete_task(p["task_id"], actor, at)
        if cmd == "reject_task":
            return agg.reject_task(p["task_id"], p["reason"],
                                   p.get("rework_craft"), actor, at, d)
        if cmd == "advance_stage":
            return agg.advance_stage(p["to_stage"], actor, at)
        if cmd == "request_design_change":
            return agg.request_design_change(
                p["new_revision"], p["elements"], p.get("embroidery_area"),
                p.get("fabric_spec"), p.get("notes"), p.get("fee_addition", 0),
                p["new_promised_date"], actor, at,
                required_crafts=p.get("required_crafts"))
        if cmd == "reconfirm":
            return agg.reconfirm(p["change_id"], p.get("measurement_version"), actor, at)
        if cmd == "decline_change":
            return agg.decline_change(p["change_id"], actor, at)
        if cmd == "schedule_fitting":
            return agg.schedule_fitting(p["scheduled_at"], p["store_id"], actor, at)
        if cmd == "mark_fitting_done":
            return agg.mark_fitting_done(p["fitting_id"], p.get("actual_at", at),
                                         p.get("note"), actor, at)
        if cmd == "mark_fitting_late":
            return agg.mark_fitting_late(p["fitting_id"], p["late_minutes"],
                                         p.get("fee_addition", 0), p["new_promised_date"],
                                         actor, at)
        if cmd == "deliver":
            return agg.deliver(actor, at)
        raise ValidationFailed(f"未知命令：{cmd}")

    # ---- 查询 ----
    def version(self, order_id):
        return self.repo.version(order_id)

    def view(self, order_id, actor_id):
        actor = self._actor(actor_id)
        state = self.repo.state(order_id)
        if actor["role"] == "artisan":
            return {"kind": "artisan", **artisan_view(state, self.dir, actor)}
        if actor["role"] == "customer" and state["customer_id"] != actor["id"]:
            raise PermissionDenied("不能查看其他顾客的订单")
        view = order_view(state, self.dir)
        view["version"] = self.repo.version(order_id)
        return {"kind": "order", **view}

    def garment(self, order_id, actor_id):
        self._actor(actor_id)
        state = self.repo.state(order_id)
        record = garment_record_view(state)
        if record is None:
            raise ValidationFailed("订单尚未交付，无成衣记录")
        return record

    def predict(self, order_id, actor_id, as_of=None):
        actor = self._actor(actor_id)
        state = self.repo.state(order_id)
        if actor["role"] == "customer" and state["customer_id"] != actor["id"]:
            raise PermissionDenied("不能查看其他顾客的订单")
        return self.scheduler.predict(state, as_of)

    def manifest(self, order_id, actor_id):
        self._actor(actor_id)
        agg = OrderAggregate(self.repo.state(order_id))
        return agg.verify_manifest()

    def board(self, actor_id):
        actor = self._actor(actor_id)
        self.dir.require_role(actor_id, {"store", "manager", "designer"})
        states = {oid: self.repo.state(oid) for oid in self.repo.list_orders()}
        # 两家门店共享订单可见性（可能共同服务同一顾客/订单）
        return store_board(states, self.dir, None)

    def directory_public(self):
        return self.dir.public()
