"""领域服务测试：冻结版本、许可分离、只追加账、最小信息与状态守卫。"""

import threading
import unittest
from datetime import datetime, timezone

from domain.app import Application
from domain.errors import (Conflict, PermissionDenied, ValidationFailed,
                          VersionConflict)
from domain.events import EventStore
from domain.order import fold, OrderAggregate
from domain.samples import build_directory
from domain.timeutil import date_in_zone, local_str


class MutableClock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def set(self, **kw):
        self.dt = self.dt.replace(**kw)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.app = Application(build_directory(), clock=self.clock)
        self.app.create_order(
            "Q-1", {"store_id": "store-soho", "customer_id": "cust-lin"}, "adv-soho")

    def cmd(self, name, payload, actor, version=None):
        return self.app.command("Q-1", name, payload, actor, version)

    def _to_confirmation(self, promised="2026-12-01",
                         crafts=("material", "suxiu", "beadwork", "cut", "tailor")):
        self.cmd("record_story",
                 {"text": "外婆的龙凤纹", "cultural_meaning": "婚嫁祝福"}, "cust-lin")
        self.cmd("grant_consent", {"scope": "social_media"}, "cust-lin")
        self.cmd("submit_design", {
            "revision": "r1", "elements": ["dragon", "phoenix"],
            "required_crafts": list(crafts),
            "embroidery_area": {"back": 0.4},
            "fabric_spec": {"silk": "19姆米真丝重绉", "color": "绛红"}}, "des-zhao")
        self.cmd("take_measurement",
                 {"data": {"bust": 88, "waist": 70, "hip": 92, "length": 120}},
                 "adv-soho")
        self.cmd("give_quote",
                 {"amount": 2800, "currency": "GBP",
                  "breakdown": {"silk": 900, "suxiu": 1400, "make": 500}}, "adv-soho")
        self.cmd("confirm", {
            "design_revision": "r1", "elements": ["dragon", "phoenix"],
            "measurement_version": 1, "promised_date": promised}, "cust-lin")

    # ---- 制作版本不可偷偷替换 ----
    def test_design_revision_cannot_be_overwritten(self):
        self._to_confirmation()
        with self.assertRaises(Conflict):
            self.cmd("submit_design",
                     {"revision": "r1", "elements": ["dragon"]}, "des-zhao")

    def test_frozen_manifest_is_intact_and_detects_tampering(self):
        self._to_confirmation()
        intact = self.app.manifest("Q-1", "mgr-qin")
        self.assertTrue(intact["intact"])
        # 直接篡改折叠状态（模拟历史被替换），哈希必须能发现
        state = self.app.repo.state("Q-1")
        state["confirmed"]["elements"] = ["dragon"]
        self.assertFalse(OrderAggregate(state).verify_manifest()["intact"])

    def test_cannot_confirm_elements_not_in_design(self):
        self._to_confirmation()
        app2 = Application(build_directory(), clock=self.clock)
        app2.create_order("Q-X", {"store_id": "store-soho", "customer_id": "cust-lin"},
                          "adv-soho")
        app2.command("Q-X", "submit_design",
                     {"revision": "r1", "elements": ["dragon"],
                      "required_crafts": ["material", "suxiu", "cut", "tailor"]},
                     "des-zhao")
        app2.command("Q-X", "take_measurement", {"data": {"bust": 88}}, "adv-soho")
        app2.command("Q-X", "give_quote", {"amount": 1000, "currency": "GBP"}, "adv-soho")
        with self.assertRaises(ValidationFailed):
            app2.command("Q-X", "confirm",
                         {"design_revision": "r1", "elements": ["phoenix"],
                          "measurement_version": 1, "promised_date": "2026-12-01"},
                         "cust-lin")

    # ---- 故事传播许可与制作合同分离 ----
    def test_withdraw_consent_does_not_touch_production_contract(self):
        self._to_confirmation()
        self.cmd("record_payment", {"amount": 2800, "currency": "GBP"}, "adv-soho")
        self.cmd("withdraw_consent", {"scope": "social_media"}, "cust-lin")
        view = self.app.view("Q-1", "adv-soho")
        self.assertEqual(view["stage"], "material")
        self.assertEqual(view["paid_total"], 2800)
        self.assertEqual(view["story"]["granted_scopes"], [])
        self.assertIn("social_media", view["story"]["withdrawn_scopes"])
        self.assertIsNone(view["story"]["text"])  # 无许可时外部视图不含正文

    def test_consent_scope_must_be_known(self):
        with self.assertRaises(ValidationFailed):
            self.cmd("grant_consent", {"scope": "billboard"}, "cust-lin")

    # ---- 收款与工时只追加 ----
    def test_payments_and_worklogs_survive_reconfirm_unchanged(self):
        self._to_confirmation()
        self.cmd("record_payment", {"amount": 2000, "currency": "GBP"}, "adv-soho")
        self.cmd("prepare_material",
                 {"batch_id": "B-1", "fabric": "重绉", "length_m": 4.0}, "mgr-qin")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd("log_work", {"task_id": "task-1", "minutes": 300}, "art-su-1")
        self.cmd("scrap_material",
                 {"batch_id": "B-1", "reason": "色差", "fee_addition": 220,
                  "new_promised_date": "2026-12-20"}, "mgr-qin")
        self.cmd("reconfirm", {"change_id": "chg-1"}, "cust-lin")
        view = self.app.view("Q-1", "mgr-qin")
        self.assertEqual(view["paid_total"], 2000)          # 已收款不变
        self.assertEqual(view["confirmed"]["price"], 3020)  # 只叠加附加费
        self.assertEqual(view["balance"], 1020)
        task = next(t for t in view["tasks"] if t["id"] == "task-1")
        self.assertEqual(task["logged_minutes"], 300)       # 已完成工时不变
        self.assertEqual(len(view["payments"]), 1)          # 无编辑/删除入口

    # ---- 最小信息 ----
    def test_artisan_sees_only_task_scope_fields(self):
        self._to_confirmation()
        self.cmd("prepare_material", {"batch_id": "B-1", "fabric": "重绉", "length_m": 4},
                 "mgr-qin")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        view = self.app.view("Q-1", "art-su-1")
        self.assertEqual(view["kind"], "artisan")
        info = view["my_tasks"][0]
        self.assertIn("motif_elements", info)
        self.assertIn("embroidery_area", info)
        for secret in ("quote", "payments", "story", "customer_id", "measurements"):
            self.assertNotIn(secret, view)
            self.assertNotIn(secret, info)
        # 裁缝任务能看到尺寸，绣娘看不到
        self.cmd("assign_task", {"craft": "cut", "assignee": "art-tailor-1"}, "mgr-qin")
        tailor = self.app.view("Q-1", "art-tailor-1")
        self.assertIn("measurements", tailor["my_tasks"][0])

    def test_skill_mismatch_blocks_assignment(self):
        self._to_confirmation()
        # 湘绣绣娘不能领苏绣
        with self.assertRaises(Conflict):
            self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-xiang-1"},
                     "mgr-qin")

    def test_artisan_cannot_touch_others_task(self):
        self._to_confirmation()
        self.cmd("prepare_material", {"batch_id": "B-1", "fabric": "重绉", "length_m": 4},
                 "mgr-qin")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        with self.assertRaises(Conflict):
            self.cmd("start_task", {"task_id": "task-1"}, "art-su-2")

    # ---- 角色边界 ----
    def test_role_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.cmd("give_quote", {"amount": 1, "currency": "GBP"}, "des-zhao")
        with self.assertRaises(PermissionDenied):
            self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"},
                     "cust-lin")

    def test_customer_isolated_to_own_orders(self):
        self._to_confirmation()
        with self.assertRaises(PermissionDenied):
            self.app.command("Q-1", "add_note", {"text": "x"}, "cust-owen")

    # ---- 返工闭环 ----
    def test_rework_links_original_and_allows_stage_advance(self):
        self._to_confirmation(crafts=("material", "beadwork", "cut", "tailor"))
        self.cmd("prepare_material", {"batch_id": "B-1", "fabric": "重绉", "length_m": 4},
                 "mgr-qin")
        self.cmd("assign_task", {"craft": "beadwork", "assignee": "art-su-2"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-1"}, "art-su-2")
        self.cmd("complete_task", {"task_id": "task-1"}, "art-su-2")
        self.cmd("reject_task",
                 {"task_id": "task-1", "reason": "金线张力不均",
                  "rework_craft": "beadwork"}, "mgr-qin")
        # 原工序 rejected 时不能推进
        with self.assertRaises(Conflict):
            self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-2"}, "art-su-2")
        self.cmd("complete_task", {"task_id": "task-2"}, "art-su-2")
        self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.assertEqual(self.app.view("Q-1", "mgr-qin")["stage"], "cutting")

    # ---- 量体复测 ----
    def test_post_confirm_remeasurement_requires_reconfirm_and_freezes_cutting(self):
        self._to_confirmation(crafts=("material", "suxiu", "cut", "tailor"))
        self.cmd("prepare_material", {"batch_id": "B-1", "fabric": "重绉", "length_m": 4},
                 "mgr-qin")
        self.cmd("take_measurement",
                 {"data": {"bust": 90, "waist": 72, "hip": 94, "length": 120},
                  "new_promised_date": "2026-12-10"}, "adv-soho")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd("complete_task", {"task_id": "task-1"}, "art-su-1")
        # 刺绣可继续，但裁剪被复测变更冻结
        with self.assertRaises(Conflict):
            self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.cmd("reconfirm", {"change_id": "chg-1", "measurement_version": 2},
                 "cust-lin")
        self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.assertEqual(self.app.view("Q-1", "mgr-qin")["confirmed"]["measurement_version"], 2)

    # ---- 迟到 ----
    def test_late_fitting_opens_change_and_blocks_delivery(self):
        self._run_to_scheduled_fitting()
        self.cmd("mark_fitting_late",
                 {"fitting_id": "fit-1", "late_minutes": 75, "fee_addition": 80,
                  "new_promised_date": "2026-12-18"}, "adv-soho")
        with self.assertRaises(Conflict):
            self.cmd("deliver", {}, "mgr-qin")
        self.cmd("reconfirm", {"change_id": "chg-1"}, "cust-lin")
        # 再试衣并交付（时钟推进到新交期之后）
        self.cmd("schedule_fitting",
                 {"scheduled_at": "2026-12-15T14:00:00+00:00",
                  "store_id": "store-soho"}, "adv-soho")
        self.cmd("mark_fitting_done", {"fitting_id": "fit-2"}, "adv-soho")
        self.clock.set(year=2026, month=12, day=20)
        self.cmd("deliver", {}, "mgr-qin")
        record = self.app.garment("Q-1", "adv-soho")
        self.assertTrue(record["delayed"])
        self.assertTrue(any("迟到" in r for r in record["delay_reasons"]))
        # 确认人链含最初确认与迟到再确认
        self.assertEqual([c["by"] for c in record["confirmer_chain"]],
                         ["cust-lin", "cust-lin"])

    def _run_to_scheduled_fitting(self, done=False):
        self._to_confirmation(promised="2026-12-01",
                              crafts=("material", "suxiu", "cut", "tailor"))
        self.cmd("prepare_material", {"batch_id": "B-1", "fabric": "重绉", "length_m": 4},
                 "mgr-qin")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd("complete_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.cmd("assign_task", {"craft": "cut", "assignee": "art-tailor-1"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-2"}, "art-tailor-1")
        self.cmd("complete_task", {"task_id": "task-2"}, "art-tailor-1")
        self.cmd("advance_stage", {"to_stage": "sewing"}, "mgr-qin")
        self.cmd("assign_task", {"craft": "tailor", "assignee": "art-tailor-2"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-3"}, "art-tailor-2")
        self.cmd("complete_task", {"task_id": "task-3"}, "art-tailor-2")
        self.cmd("schedule_fitting",
                 {"scheduled_at": "2026-11-28T14:00:00+00:00",
                  "store_id": "store-soho"}, "adv-soho")
        if done:
            self.cmd("mark_fitting_done", {"fitting_id": "fit-1"}, "adv-soho")

    # ---- 顾客改稿：再确认后重做，费用日期更新，旧账不动 ----
    def test_customer_design_change_redoes_embroidery_after_reconfirm(self):
        self._to_confirmation(crafts=("material", "suxiu", "cut", "tailor"))
        self.cmd("prepare_material", {"batch_id": "B-1", "fabric": "重绉", "length_m": 4},
                 "mgr-qin")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd("log_work", {"task_id": "task-1", "minutes": 200}, "art-su-1")
        self.cmd("complete_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")

        # 顾客在裁剪阶段改稿：新图案需要苏绣重做
        self.cmd("request_design_change", {
            "new_revision": "r2", "elements": ["phoenix", "peony"],
            "required_crafts": ["material", "suxiu", "cut", "tailor"],
            "notes": "希望加牡丹", "fee_addition": 350,
            "new_promised_date": "2026-12-20"}, "cust-lin")
        with self.assertRaises(Conflict):
            self.cmd("advance_stage", {"to_stage": "sewing"}, "mgr-qin")
        self.cmd("reconfirm", {"change_id": "chg-1"}, "cust-lin")

        view = self.app.view("Q-1", "mgr-qin")
        self.assertEqual(view["stage"], "embroidery")      # 回到刺绣重做
        self.assertEqual(view["confirmed"]["design_revision"], "r2")
        self.assertEqual(view["confirmed"]["price"], 3150)
        self.assertEqual(view["confirmed"]["promised_date"], "2026-12-20")
        # 旧工时保留
        self.assertEqual(view["tasks"][0]["logged_minutes"], 200)

        # 必须在新版本下重做刺绣，不能凭旧完工记录直接推进
        with self.assertRaises(Conflict):
            self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.cmd("assign_task", {"craft": "suxiu", "assignee": "art-su-2"}, "mgr-qin")
        self.cmd("start_task", {"task_id": "task-2"}, "art-su-2")
        self.cmd("complete_task", {"task_id": "task-2"}, "art-su-2")
        self.cmd("advance_stage", {"to_stage": "cutting"}, "mgr-qin")

    # ---- 交期预测：节假日 / 并行 / 技能 ----
    def test_prediction_accounts_for_national_day_holidays(self):
        self.clock.set(year=2026, month=9, day=28)
        self._to_confirmation(promised="2026-12-01")
        pred = self.app.predict("Q-1", "mgr-qin")
        holiday_dates = {h.split(" ")[0] for h in pred["holidays_on_path"]}
        self.assertTrue({"2026-10-01", "2026-10-02"} <= holiday_dates)
        self.assertIn("国庆节", " ".join(pred["holidays_on_path"]))

    def test_prediction_warns_on_single_skill_holder(self):
        self._to_confirmation(crafts=("material", "xiangxiu", "cut", "tailor"))
        pred = self.app.predict("Q-1", "mgr-qin")
        self.assertTrue(any("xiangxiu" in w for w in pred["warnings"]))

    def test_parallel_embroidery_group_takes_longest_branch(self):
        self._to_confirmation(crafts=("material", "suxiu", "beadwork", "cut", "tailor"))
        pred = self.app.predict("Q-1", "mgr-qin")
        by_craft = {n["craft"]: n for n in pred["nodes"] if "end" in n}
        # 苏绣 12 天与钉珠 4 天并行：钉珠必须早于苏绣结束
        self.assertLess(by_craft["beadwork"]["end"], by_craft["suxiu"]["end"])

    # ---- 跨时区确认 ----
    def test_confirmation_recorded_utc_and_localized(self):
        self._to_confirmation()
        state = self.app.repo.state("Q-1")
        at = state["confirmed"]["at"]
        # UTC 20:00：伦敦同日，上海次日
        dt = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc).isoformat()
        self.assertEqual(date_in_zone(dt, "Europe/London").isoformat(), "2026-10-20")
        self.assertEqual(date_in_zone(dt, "Asia/Shanghai").isoformat(), "2026-10-21")
        self.assertIn("London", local_str(dt, "Europe/London"))
        self.assertTrue(at.endswith("+00:00"))


class EventStoreConcurrencyTest(unittest.TestCase):
    def test_optimistic_concurrency_one_winner(self):
        store = EventStore()
        store.append("s", [{"type": "T", "data": {}}], -1)

        results = []

        def append_event(label):
            try:
                store.append("s", [{"type": "T", "data": {"by": label}}], 1)
                results.append(("ok", label))
            except VersionConflict:
                results.append(("conflict", label))

        threads = [threading.Thread(target=append_event, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r[0] == "ok"), 1)
        self.assertEqual(sum(1 for r in results if r[0] == "conflict"), 4)


if __name__ == "__main__":
    unittest.main()
