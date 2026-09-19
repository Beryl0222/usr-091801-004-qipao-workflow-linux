"""领域规则单元测试：双授权、锁定版本、变更单、只增账本、最小信息。"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qipao import OrderService, OrderRepository, FixedClock
from qipao.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from tests.helpers import (
    ELEMENTS_PEONY, MEASUREMENTS_V1, MEASUREMENTS_V2, QUOTE_2200, START,
    V, build_service, open_order,
)


class StoryConsentTest(unittest.TestCase):
    def test_withdrawing_publication_does_not_stop_production(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v, with_publication=("workshop_internal",
                                                   "store_marketing"))
        # 顾客撤回全部传播许可
        _, v.v = svc.set_publication(oid, "p_amelia", "workshop_internal",
                                     False, v.v, reason="不想公开家事后")
        _, v.v = svc.set_publication(oid, "p_amelia", "store_marketing",
                                     False, v.v)
        # 已确认的制作合同不受影响：刺绣仍可开工
        emb = f"{oid}_embroidery_1"
        _, v.v = svc.start_task(oid, "p_wang", emb, v.v)
        # 绣娘的文化含义提示随撤回消失（任务在制，任务卡仍下发）
        card, _ = svc.view_for(oid, "p_wang")
        self.assertNotIn("craft_note", card["tasks"][0])
        _, v.v = svc.complete_task(oid, "p_wang", emb, v.v)
        # 公开视图不可发布，但授权历史完整留痕
        story = svc.garment(oid)["public_story"]
        self.assertFalse(story["publishable"])
        self.assertEqual(story["reason"], "revoked")
        self.assertIsNone(story["content"])
        self.assertTrue(any(g["granted"] is False for g in story["history"]))

    def test_withdrawn_recording_consent_blocks_new_version_approval(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        _, v.v = svc.set_consent(oid, "p_amelia", False, v.v)
        _, v.v = svc.submit_draft(
            oid, "p_chen", ELEMENTS_PEONY, "suzhou", v.v,
            revision_of=f"{oid}_draft_1")
        with self.assertRaises(ConflictError):
            svc.approve_version(oid, "p_amelia", f"{oid}_draft_2", 1,
                                QUOTE_2200, "silk_2026_09_a", v.v)

    def test_only_customer_manages_publication(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        with self.assertRaises(ValidationError):
            svc.set_publication(oid, "p_grace", "store_marketing", False, v.v)

    def test_anonymous_only_hides_identity(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v, with_publication=("public_anonymous",))
        story = svc.garment(oid)["public_story"]
        self.assertTrue(story["publishable"])
        self.assertEqual(story["attribution"], "anonymous")
        self.assertIsNone(story["customer_label"])
        self.assertIn("牡丹", story["content"])


class LockedVersionTest(unittest.TestCase):
    def test_fabric_swap_without_change_is_rejected(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        order = svc.get(oid)
        with self.assertRaises(ConflictError):
            order.verify_production_basis(fabric_batch_id="silk_2026_09_b")

    def test_confirmed_redesign_creates_new_fingerprint_keeps_old(self):
        svc, clock = build_service()
        v = V(0)
        oid = open_order(svc, v)
        old_fp = svc.get(oid).current_version.fingerprint
        # 顾客改稿：袖口由盖袖改为长袖，加费 300 镑、延 3 个工作日
        new_elements = [e for e in ELEMENTS_PEONY if e["kind"] != "sleeve"]
        new_elements.append({"kind": "sleeve", "value": "long", "label": "长袖"})
        co_id, v.v = svc.raise_change(
            oid, "p_chen", "customer_redesign",
            "顾客希望改长袖以参加冬季仪式", "embroidery",
            300.0, 3.0, v.v, changes={"elements": new_elements})
        # 变更未确认时，刺绣工序锁定
        with self.assertRaises(ConflictError):
            svc.start_task(oid, "p_wang", f"{oid}_embroidery_1", v.v)
        _, v.v = svc.confirm_change(oid, "p_amelia", co_id, v.v)
        order = svc.get(oid)
        self.assertEqual(len(order.approved_versions), 2)
        self.assertNotEqual(order.current_version.fingerprint, old_fp)
        self.assertEqual(order.approved_versions[0].fingerprint, old_fp)
        self.assertEqual(order.current_version.change_order_id, co_id)
        self.assertEqual(order.current_version.supersedes, old_fp)
        # 新元素进入锁定版本
        self.assertIn({"kind": "sleeve", "value": "long", "label": "长袖"},
                      order.current_version.elements)

    def test_remeasure_after_approval_requires_reconfirmation(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        mv, v.v = svc.add_measurement(
            oid, "p_grace", MEASUREMENTS_V2, v.v,
            remeasure_reason="顾客近一个月健身，肩背尺寸微调")
        order = svc.get(oid)
        with self.assertRaises(ConflictError):
            order.verify_production_basis(measurement_version=mv)
        # 历史量体版本两版都在
        staff, _ = svc.view_for(oid, "p_grace")
        self.assertEqual([m["version"] for m in staff["measurements"]], [1, 2])

    def test_scrapped_batch_must_be_replaced_before_customer_confirms(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        co_id, v.v = svc.scrap_material(
            oid, "p_lin", "silk_2026_09_a", "绸缎抽丝检验不合格", v.v)
        with self.assertRaises(ConflictError):
            svc.confirm_change(oid, "p_amelia", co_id, v.v)
        _, v.v = svc.amend_change(
            oid, "p_lin", co_id,
            {"fabric_batch_id": "silk_2026_09_b"},
            cost_delta=120.0, expected_version=v.v)
        _, v.v = svc.confirm_change(oid, "p_amelia", co_id, v.v)
        self.assertEqual(
            svc.get(oid).current_version.fabric_batch_id, "silk_2026_09_b")


class LedgerImmutabilityTest(unittest.TestCase):
    def test_payments_and_work_hours_survive_later_changes(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        emb = f"{oid}_embroidery_1"
        _, v.v = svc.start_task(oid, "p_wang", emb, v.v)
        _, v.v = svc.log_work(oid, "p_wang", emb, 42, 45, v.v)
        _, v.v = svc.complete_task(oid, "p_wang", emb, v.v)
        before = svc.get(oid).ledger_totals()
        # 改稿补费
        co_id, v.v = svc.raise_change(
            oid, "p_chen", "customer_redesign", "加绣一对蝴蝶", "embroidery",
            300.0, 2.0, v.v, changes={"elements": ELEMENTS_PEONY})
        _, v.v = svc.confirm_change(oid, "p_amelia", co_id, v.v)
        _, v.v = svc.record_payment(oid, "p_grace", 300, v.v, note="改稿补款")
        after = svc.get(oid).ledger_totals()
        # 已收款 1100 与已完成工时 42h 原样保留
        self.assertEqual(after["payments"], before["payments"] + 300)
        self.assertEqual(after["work_hours"], before["work_hours"])
        self.assertEqual(after["charges"], before["charges"] + 300)
        # 账本条目只增：序列连续，无删除
        seqs = [e.seq for e in svc.get(oid).ledger]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class MinimalDisclosureTest(unittest.TestCase):
    def test_embroiderer_sees_only_need_to_know_fields(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v, with_publication=("workshop_internal",))
        raw = svc.view_for(oid, "p_wang")[0]
        blob = repr(raw)
        self.assertNotIn("p_amelia", blob)
        self.assertNotIn("Amelia", blob)
        self.assertNotIn("2200", blob)
        self.assertNotIn("waist", blob)
        card = raw["tasks"][0]
        self.assertEqual(card["stage"], "embroidery")
        self.assertIn("locked_fingerprint", card)
        self.assertEqual(card["embroidery_style"], "suzhou")
        # 与绣工无关的元素不下发
        kinds = {e["kind"] for e in card["elements"]}
        self.assertIn("motif", kinds)
        self.assertNotIn("fastener", kinds)
        self.assertEqual(card["craft_note"],
                         "牡丹象征家族记忆与祝福，是祖母出嫁时嫁衣上的纹样。")

    def test_tailor_sees_measurements_but_not_money_or_story(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        raw = svc.view_for(oid, "p_zhao")[0]
        blob = repr(raw)
        self.assertNotIn("2200", blob)
        self.assertNotIn("祖母", blob)
        card = raw["tasks"][0]
        self.assertEqual(card["measurements"]["waist"], 70)

    def test_unassigned_artisan_has_no_tasks(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        raw, version = svc.view_for(oid, "p_li")
        self.assertEqual(raw["tasks"], [])
        self.assertEqual(version, svc.get(oid).version)


class ReworkTest(unittest.TestCase):
    def test_rework_links_to_original_and_blocks_downstream(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        cut = f"{oid}_cutting_1"
        _, v.v = svc.start_task(oid, "p_zhao", cut, v.v)
        _, v.v = svc.complete_task(oid, "p_zhao", cut, v.v)
        # 裁剪纹路偏差，主管要求返工
        rework_id, v.v = svc.request_rework(
            oid, "p_lin", cut, "斜裁方向与样板相反", v.v)
        order = svc.get(oid)
        orig = next(t for t in order.tasks if t.id == cut)
        self.assertEqual(orig.status, "rework_required")
        rw = next(t for t in order.tasks if t.id == rework_id)
        self.assertEqual(rw.rework_of, cut)
        self.assertEqual(rw.attempt, 2)
        # 缝制依赖裁剪，返工未完不能派工缝制
        with self.assertRaises(ConflictError):
            svc.assign_task(oid, "p_lin", "tailoring", "p_zhao", v.v)


class ParallelStageTest(unittest.TestCase):
    def test_pending_change_on_embroidery_does_not_freeze_parallel_cutting(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        _, vv = svc.raise_change(
            oid, "p_chen", "customer_redesign", "改袖", "embroidery",
            300, 3, v.v, changes={"elements": ELEMENTS_PEONY})
        # 刺绣锁定
        with self.assertRaises(ConflictError):
            svc.start_task(oid, "p_wang", f"{oid}_embroidery_1", vv)
        # 与刺绣并行的裁剪不受影响（共享备料、互不依赖）
        _, nv = svc.start_task(oid, "p_zhao", f"{oid}_cutting_1", vv)
        self.assertGreater(nv, vv)


class TimezoneConfirmationTest(unittest.TestCase):
    def test_confirmation_records_customer_local_instant(self):
        svc, clock = build_service("2026-09-21T21:30:00Z")  # 伦敦夏令时 22:30
        v = V(0)
        oid = open_order(svc, v)
        order = svc.get(oid)
        approved = order.current_version
        self.assertEqual(approved.customer_zone, "Europe/London")
        self.assertTrue(approved.approved_at.startswith("2026-09-21T21:30:00"))
        self.assertTrue(approved.approved_local.startswith("2026-09-21T22:30:00"))
        self.assertIn("+01:00", approved.approved_local)


if __name__ == "__main__":
    unittest.main()
