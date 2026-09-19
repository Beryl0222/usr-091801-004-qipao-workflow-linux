"""应用服务：把用例命令经仓储乐观锁通道作用于订单聚合。

每个写方法都要求 expected_version（创建除外），返回 (结果, 新版本号)。
读方法返回按角色裁剪的投影。
"""

import copy
import uuid

from .catalog import REGISTRY
from .clock import SystemClock
from .order import Order
from .projections import artisan_view, garment_record, staff_view
from .scheduler import Scheduler


class OrderService:
    def __init__(self, repo, clock=None, scheduler=None, fabric_batches=None):
        self.repo = repo
        self.clock = clock or SystemClock()
        self.scheduler = scheduler or Scheduler(self.clock)
        # 面料批次为跨订单共享的库存台账：服务级持有，默认从目录拷贝
        self.fabric_batches = fabric_batches or copy.deepcopy(
            REGISTRY["fabric_batches"])

    # -- 读 -----------------------------------------------------------------

    def get(self, order_id):
        return self.repo.get(order_id)

    def view_for(self, order_id, actor):
        order = self.repo.get(order_id)
        from .catalog import person
        p = person(actor)
        role = p.role if p else "unknown"
        if role in ("embroiderer", "tailor"):
            return artisan_view(order, actor), order.version
        return staff_view(order, actor), order.version

    def garment(self, order_id):
        return garment_record(self.repo.get(order_id))

    def schedule(self, order_id, include_pending=False):
        return self.scheduler.predict(
            self.repo.get(order_id),
            include_pending_changes=include_pending,
        )

    # -- 写：统一通道 --------------------------------------------------------

    def _cmd(self, order_id, expected_version, fn):
        return self.repo.mutate(order_id, expected_version, fn)

    def create_order(self, customer_id, store_id, actor):
        order_id = f"order_{uuid.uuid4().hex[:10]}"
        order = Order(order_id, customer_id, store_id, actor, clock=self.clock)
        self.repo.create(order)
        return order_id, order.version

    def add_store(self, order_id, actor, store_id, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.add_store(actor, store_id))

    def record_story(self, order_id, actor, customer, content, significance,
                     expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.record_story(actor, customer, content, significance))

    def set_consent(self, order_id, actor, granted, expected_version, reason=""):
        return self._cmd(order_id, expected_version,
                         lambda o: o.consent_recording(actor, granted, reason))

    def set_publication(self, order_id, actor, scope, granted, expected_version, reason=""):
        return self._cmd(order_id, expected_version,
                         lambda o: o.set_publication(actor, scope, granted, reason))

    def add_measurement(self, order_id, actor, points, expected_version,
                        note="", remeasure_reason=""):
        return self._cmd(order_id, expected_version,
                         lambda o: o.add_measurement(actor, points, note, remeasure_reason))

    def submit_draft(self, order_id, actor, elements, embroidery_style,
                     expected_version, inspiration="", revision_of=None):
        return self._cmd(order_id, expected_version,
                         lambda o: o.submit_draft(
                             actor, elements, embroidery_style, inspiration, revision_of))

    def approve_version(self, order_id, actor, draft_id, measurement_version, quote,
                        fabric_batch_id, expected_version, currency="GBP"):
        return self._cmd(order_id, expected_version,
                         lambda o: o.approve_version(
                             actor, draft_id, measurement_version, quote,
                             fabric_batch_id, currency))

    def raise_change(self, order_id, actor, reason, summary, affected_stage,
                     cost_delta, extra_work_days, expected_version, changes=None,
                     currency="GBP"):
        return self._cmd(order_id, expected_version,
                         lambda o: o.raise_change_order(
                             actor, reason, summary, affected_stage,
                             cost_delta, extra_work_days, changes, currency))

    def confirm_change(self, order_id, actor, change_id, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.confirm_change_order(actor, change_id))

    def reject_change(self, order_id, actor, change_id, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.reject_change_order(actor, change_id))

    def allocate_material(self, order_id, actor, batch_id, meters, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.allocate_material(
                             actor, batch_id, meters, self.fabric_batches))

    def amend_change(self, order_id, actor, change_id, changes, cost_delta,
                     expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.amend_change_order(
                             actor, change_id, changes, cost_delta))

    def scrap_material(self, order_id, actor, batch_id, reason, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.scrap_material(
                             actor, batch_id, reason, self.fabric_batches))

    def assign_task(self, order_id, actor, stage, assignee, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.assign_task(actor, stage, assignee))

    def start_task(self, order_id, actor, task_id, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.start_task(actor, task_id))

    def complete_task(self, order_id, actor, task_id, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.complete_task(actor, task_id))

    def request_rework(self, order_id, actor, task_id, reason, expected_version):
        return self._cmd(order_id, expected_version,
                         lambda o: o.request_rework(actor, task_id, reason))

    def log_work(self, order_id, actor, task_id, hours, hourly_rate,
                 expected_version, note=""):
        return self._cmd(order_id, expected_version,
                         lambda o: o.log_work(actor, task_id, hours, hourly_rate, note))

    def record_payment(self, order_id, actor, amount, expected_version,
                       currency="GBP", note=""):
        return self._cmd(order_id, expected_version,
                         lambda o: o.record_payment(actor, amount, currency, note))

    def schedule_fitting(self, order_id, actor, when, expected_version, store_id=None):
        return self._cmd(order_id, expected_version,
                         lambda o: o.schedule_fitting(actor, when, store_id))

    def mark_late(self, order_id, actor, fitting_id, reschedule_at, expected_version,
                  penalty=0.0, extra_work_days=0.0):
        return self._cmd(order_id, expected_version,
                         lambda o: o.mark_customer_late(
                             actor, fitting_id, reschedule_at, penalty, extra_work_days))

    def record_fitting(self, order_id, actor, fitting_id, result, expected_version,
                       notes=""):
        return self._cmd(order_id, expected_version,
                         lambda o: o.record_fitting(actor, fitting_id, result, notes))

    def deliver(self, order_id, actor, expected_version):
        return self._cmd(order_id, expected_version, lambda o: o.deliver(actor))
