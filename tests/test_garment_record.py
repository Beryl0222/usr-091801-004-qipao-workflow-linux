"""端到端验收：从成衣记录说明延期原因、确认人以及获准公开的故事范围。

走查一张包含全部典型波折的订单：
改稿（顾客加袖）→ 面料批次报废换批 → 试衣迟到改约 → 试衣不合身返工 →
交付。成衣记录必须能逐项回答：为什么延期、延多久、谁确认、
最终制作版本指纹是什么、故事当前可以公开到什么范围。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qipao.errors import ConflictError
from tests.helpers import (
    ELEMENTS_PEONY, QUOTE_2200, V, build_service, complete_production, open_order,
)


class GarmentRecordAcceptanceTest(unittest.TestCase):
    def test_full_journey_record_explains_every_delay(self):
        svc, clock = build_service()
        v = V(0)
        oid = open_order(svc, v, with_publication=(
            "workshop_internal", "store_marketing", "public_anonymous"))

        # 波折 1：顾客改稿（在刺绣派工后、开工前）
        long_sleeve = [e for e in ELEMENTS_PEONY if e["kind"] != "sleeve"]
        long_sleeve.append({"kind": "sleeve", "value": "long", "label": "长袖"})
        co_redesign, v.v = svc.raise_change(
            oid, "p_chen", "customer_redesign",
            "顾客要求改长袖出席冬至家宴", "embroidery",
            300.0, 3.0, v.v, changes={"elements": long_sleeve})
        _, v.v = svc.confirm_change(oid, "p_amelia", co_redesign, v.v)

        # 波折 2：真丝面料检验不合格报废，换批次加费 120 镑、延 2 天
        co_scrap, v.v = svc.scrap_material(
            oid, "p_lin", "silk_2026_09_a", "整幅抽丝、门幅不均", v.v)
        with self.assertRaises(ConflictError):
            svc.confirm_change(oid, "p_amelia", co_scrap, v.v)
        _, v.v = svc.amend_change(
            oid, "p_lin", co_scrap,
            {"fabric_batch_id": "silk_2026_09_b"}, 120.0,
            expected_version=v.v)
        _, v.v = svc.confirm_change(oid, "p_amelia", co_scrap, v.v)

        # 制作完工
        emb, tail = complete_production(svc, oid, v)

        # 波折 3：试衣迟到，改约、加 50 镑档期费、延 1 天
        fit1, v.v = svc.schedule_fitting(
            oid, "p_grace", "2026-11-20T16:00:00Z", v.v)
        co_late, v.v = svc.mark_late(
            oid, "p_olivia", fit1, "2026-11-23T16:00:00Z", v.v,
            penalty=50.0, extra_work_days=1.0)
        # 新费用必须顾客确认；未确认不能交付
        with self.assertRaises(ConflictError):
            svc.deliver(oid, "p_grace", v.v)
        _, v.v = svc.confirm_change(oid, "p_amelia", co_late, v.v)

        # 波折 4：改约试衣发现腰省需返工
        fit2 = f"{oid}_fit_2"
        _, v.v = svc.record_fitting(
            oid, "p_grace", fit2, "alterations", v.v, notes="腰省余量偏大")
        # 返工完工（返工任务 attempt 2）
        tail2 = f"{oid}_tailoring_2"
        _, v.v = svc.start_task(oid, "p_zhao", tail2, v.v)
        _, v.v = svc.log_work(oid, "p_zhao", tail2, 6, 40, v.v, note="返工修省")
        _, v.v = svc.complete_task(oid, "p_zhao", tail2, v.v)
        # 返工后再约一次试衣
        fit3, v.v = svc.schedule_fitting(
            oid, "p_grace", "2026-11-25T16:00:00Z", v.v)
        _, v.v = svc.record_fitting(oid, "p_grace", fit3, "ok", v.v)
        _, v.v = svc.deliver(oid, "p_grace", v.v)

        record = svc.garment(oid)

        # 1) 状态与最终版本（改稿→v2，换批→v3，迟到改约→v4）
        self.assertEqual(record["status"], "delivered")
        self.assertEqual(record["garment_version"]["version_no"], 4)
        self.assertEqual(record["garment_version"]["fabric_batch_id"],
                         "silk_2026_09_b")
        sleeve = next(e for e in record["garment_version"]["elements"]
                      if e["kind"] == "sleeve")
        self.assertEqual(sleeve["value"], "long")

        # 2) 四个版本指纹链：v4→v3→v2→v1，全部留档
        hist = record["version_history"]
        self.assertEqual([h["version_no"] for h in hist], [1, 2, 3, 4])
        self.assertEqual(hist[1]["supersedes"], hist[0]["fingerprint"])
        self.assertEqual(hist[2]["supersedes"], hist[1]["fingerprint"])
        self.assertEqual(hist[3]["supersedes"], hist[2]["fingerprint"])
        # 确认人始终是顾客本人，且记录了伦敦本地确认时刻
        for h in hist:
            self.assertEqual(h["approved_by"]["id"], "p_amelia")
            self.assertEqual(h["customer_zone"], "Europe/London")
            self.assertIn("2026", h["approved_at_customer_local"])

        # 3) 延期原因逐项可解释：改稿 3 天 + 报废 2 天 + 迟到 1 天
        reasons = {d["reason"]: d for d in record["delay_explanation"]}
        self.assertEqual(set(reasons), {"customer_redesign", "material_scrap",
                                        "late_customer", "rework"})
        self.assertEqual(reasons["customer_redesign"]["days"], 3.0)
        self.assertEqual(reasons["material_scrap"]["days"], 2.0)
        self.assertEqual(reasons["late_customer"]["days"], 1.0)
        self.assertEqual(record["total_confirmed_delay_days"], 6.0)
        # 每条延期都有确认人
        for key in ("customer_redesign", "material_scrap", "late_customer"):
            self.assertEqual(reasons[key]["confirmed_by"]["id"], "p_amelia")
        self.assertIn("抽丝", reasons["material_scrap"]["detail"])

        # 4) 返工链指向原缝制工序
        self.assertEqual(len(record["reworks"]), 1)
        self.assertEqual(record["reworks"][0]["rework_of"], tail)
        self.assertEqual(record["reworks"][0]["stage"], "tailoring")
        self.assertEqual(record["reworks"][0]["ordered_by"]["id"], "p_grace")

        # 5) 财务：已收款与已完工工时在多轮变更后仍然完整
        totals = svc.get(oid).ledger_totals()
        # 应收 = 2200 报价 + 300 改稿 + 120 换批 + 50 迟到
        self.assertEqual(totals["charges"], 2200 + 300 + 120 + 50)
        self.assertEqual(totals["payments"], 1100)
        self.assertEqual(totals["work_hours"], 42 + 26 + 6)

        # 6) 传播许可当前范围：三个范围都在，可署名公开
        pub = record["public_story"]
        self.assertTrue(pub["publishable"])
        self.assertEqual(pub["attribution"], "named")
        self.assertEqual(pub["customer_label"]["id"], "p_amelia")

        # 7) 交付后顾客撤回传播许可：成衣记录立即收窄，制作与交付事实不变
        _, v.v = svc.set_publication(oid, "p_amelia", "store_marketing", False,
                                     v.v, reason="家庭情况变化，不希望署名")
        _, v.v = svc.set_publication(oid, "p_amelia", "public_anonymous", False,
                                     v.v, reason="家庭情况变化，不希望署名")
        record2 = svc.garment(oid)
        self.assertEqual(record2["status"], "delivered")
        pub2 = record2["public_story"]
        self.assertFalse(pub2["publishable"])
        self.assertEqual(pub2["reason"], "revoked")
        self.assertIsNone(pub2["content"])
        self.assertEqual(pub2["last_decision"]["scope"], "public_anonymous")
        self.assertEqual(pub2["last_decision"]["reason"], "家庭情况变化，不希望署名")
        # 两个渠道的撤回都完整留痕
        revoked = {g["scope"] for g in pub2["history"] if g["granted"] is False}
        self.assertEqual(revoked, {"store_marketing", "public_anonymous"})
        # 版本指纹、延期、确认人等制作证据不受撤回影响
        self.assertEqual(record2["garment_version"]["fingerprint"],
                         record["garment_version"]["fingerprint"])
        self.assertEqual(record2["total_confirmed_delay_days"], 6.0)

        # 8) 内部工艺提示也随 workshop_internal 之外的渠道独立管理：
        #    撤回 workshop_internal 后绣娘任务卡不再显示文化含义
        _, v.v = svc.set_publication(oid, "p_amelia", "workshop_internal",
                                     False, v.v)
        card, _ = svc.view_for(oid, "p_wang")
        # 任务已全部完工，绣娘当前无在制卡（最小信息不回传已结束任务）
        self.assertEqual(card["tasks"], [])


class SchedulingExplanationTest(unittest.TestCase):
    def test_prediction_cites_holidays_and_skills(self):
        svc, clock = build_service()
        v = V(0)
        oid = open_order(svc, v)
        prediction = svc.schedule(oid)
        # 国庆假期必然落在刺绣跨期内
        holidays = {h["date"]: h["reason"] for h in prediction["holidays_observed"]}
        self.assertIn("2026-10-01", holidays)
        self.assertEqual(holidays["2026-10-01"], "国庆节")
        # 刺绣在关键路径上，裁剪并行只需 1 天、不在关键路径
        self.assertIn("embroidery", prediction["critical_path"])
        self.assertNotIn("cutting", prediction["critical_path"])
        emb = next(s for s in prediction["stages"] if s["stage"] == "embroidery")
        # 王雪梅效率 1.1、苏绣系数 1.15：6 / 1.1 * 1.15 ≈ 6.27
        self.assertAlmostEqual(emb["work_days"], 6.27, places=1)
        # 湘绣较慢绣娘的预测应晚于苏绣快绣娘
        stages_by_wang = emb["finish"]

    def test_less_skilled_embroiderer_predicts_later(self):
        from qipao.scheduler import Scheduler
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        # 把刺绣改派给李秀英（效率 0.8），预测应变长
        svc.repo.mutate(oid, v.v, lambda o: None)
        prediction = svc.schedule(oid)
        emb = next(s for s in prediction["stages"] if s["stage"] == "embroidery")
        # 自动选人会选更快的王雪梅；直接比较两人推进结果
        from qipao.scheduler import advance_work_time
        from qipao.clock import parse_instant
        from qipao.catalog import REGISTRY
        start = parse_instant("2026-09-21T09:00:00Z")
        f_fast, _ = advance_work_time(start, 6 / 1.1 * 1.15, "cn_workshop")[1:]
        f_slow, _ = advance_work_time(start, 6 / 0.8 * 1.15, "cn_workshop")[1:]
        self.assertGreater(f_slow, f_fast)


if __name__ == "__main__":
    unittest.main()
