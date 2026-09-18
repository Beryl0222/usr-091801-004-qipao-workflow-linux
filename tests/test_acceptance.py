"""首批上线验收：两家门店同时改同一订单 + 成衣记录可说明延期、确认人与故事范围。

通过真实 HTTP（ThreadingHTTPServer + urllib）演练：
- 两店基于同一版本并发提交：恰好一方成功，另一方收到 409 version_conflict；
- 失败方刷新版本后重试成功；
- 成衣记录含延期原因、确认人链与当前获准公开的故事范围；
- 绣娘接口只暴露任务最小信息。
"""

import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain.app import Application
from domain.httpapi import make_handler
from domain.samples import build_directory


class MutableClock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def set(self, **kw):
        self.dt = self.dt.replace(**kw)


class AcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = MutableClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        cls.app = Application(build_directory(), clock=cls.clock)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    # ---- HTTP 辅助 ----
    def req(self, method, path, actor=None, body=None, expected_version=None):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor"] = actor
        if expected_version is not None:
            headers["X-Expected-Version"] = str(expected_version)
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as err:
            return err.code, json.load(err)

    def cmd(self, oid, name, body, actor, version=None):
        return self.req("POST", f"/orders/{oid}/commands/{name}", actor, body, version)

    def view(self, oid, actor):
        return self.req("GET", f"/orders/{oid}", actor)

    def _build_confirmed_order(self, oid, promised="2026-12-10",
                               crafts=("material", "suxiu", "cut", "tailor")):
        self.req("POST", "/orders", "adv-soho",
                 {"order_id": oid, "store_id": "store-soho", "customer_id": "cust-lin"})
        self.cmd(oid, "record_story",
                 {"text": "外婆留下的缠枝莲", "cultural_meaning": "绵延不绝的家族祝愿"},
                 "cust-lin")
        self.cmd(oid, "grant_consent", {"scope": "social_media"}, "cust-lin")
        self.cmd(oid, "grant_consent", {"scope": "store_showcase"}, "cust-lin")
        self.cmd(oid, "submit_design",
                 {"revision": "r1", "elements": ["lotus-vine"],
                  "required_crafts": list(crafts),
                  "fabric_spec": {"silk": "素绉缎"}}, "des-zhao")
        self.cmd(oid, "take_measurement", {"data": {"bust": 88, "length": 120}}, "adv-soho")
        self.cmd(oid, "give_quote", {"amount": 2600, "currency": "GBP"}, "adv-soho")
        self.cmd(oid, "confirm",
                 {"design_revision": "r1", "elements": ["lotus-vine"],
                  "measurement_version": 1, "promised_date": promised}, "cust-lin")
        self.cmd(oid, "record_payment", {"amount": 1300, "currency": "GBP"}, "adv-soho")

    # ========== 验收一：两店并发同改同一订单 ==========
    def test_two_stores_concurrent_edit_one_wins_one_retries(self):
        oid = "Q-CONFLICT"
        self._build_confirmed_order(oid)
        self.cmd(oid, "prepare_material",
                 {"batch_id": "B-1", "fabric": "素绉缎", "length_m": 4}, "mgr-qin")

        # 两家门店分别读取，拿到相同版本
        _, v_soho = self.view(oid, "adv-soho")
        _, v_shore = self.view(oid, "adv-shore")
        self.assertEqual(v_soho["version"], v_shore["version"])
        base_version = v_soho["version"]

        outcomes = {}

        def store_edit(actor, key):
            status, payload = self.cmd(
                oid, "add_note", {"text": f"{key} 门店电话回访记录"}, actor, base_version)
            outcomes[key] = (status, payload)

        t1 = threading.Thread(target=store_edit, args=("adv-soho", "soho"))
        t2 = threading.Thread(target=store_edit, args=("adv-shore", "shore"))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(outcomes[k][0] for k in outcomes)
        self.assertEqual(statuses, [200, 409])
        # 失败的一方必须是版本冲突错误
        loser_key = next(k for k in outcomes if outcomes[k][0] == 409)
        self.assertEqual(outcomes[loser_key][1]["error"], "version_conflict")
        self.assertEqual(outcomes[loser_key][1]["actual_version"], base_version + 1)

        # 失败门店刷新后拿到新版本并重试成功
        _, refreshed = self.view(oid, "adv-shore" if loser_key == "shore" else "adv-soho")
        self.assertEqual(refreshed["version"], base_version + 1)
        retry_actor = "adv-shore" if loser_key == "shore" else "adv-soho"
        status, payload = self.cmd(
            oid, "add_note", {"text": "失败门店刷新后重试"}, retry_actor,
            refreshed["version"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], base_version + 2)

        # 两条备注都在，谁也没有覆盖谁
        _, final = self.view(oid, "adv-soho")
        self.assertEqual(len(final["notes"]), 2)

    # ========== 验收二：成衣记录说明延期原因、确认人与公开故事范围 ==========
    def test_garment_record_explains_delay_confirmer_and_story_scope(self):
        oid = "Q-GARMENT"
        self._build_confirmed_order(oid, promised="2026-11-20")
        self.cmd(oid, "prepare_material",
                 {"batch_id": "B-7", "fabric": "素绉缎", "length_m": 4}, "mgr-qin")

        # 材料报废 → 变更单（费用与日期重谈）→ 顾客再确认
        self.cmd(oid, "scrap_material",
                 {"batch_id": "B-7", "reason": "验布发现明显色差与跳丝",
                  "fee_addition": 260, "new_promised_date": "2026-12-05"}, "mgr-qin")
        status, _ = self.cmd(oid, "reconfirm", {"change_id": "chg-1"}, "cust-lin")
        self.assertEqual(status, 200)

        # 主管登记替代批次，重新通过备料检验后才进入刺绣
        status, _ = self.cmd(oid, "prepare_material",
                             {"batch_id": "B-8", "fabric": "素绉缎（替代批）", "length_m": 4},
                             "mgr-qin")
        self.assertEqual(status, 200)

        # 生产推进：刺绣 → 裁剪 → 缝制
        self.cmd(oid, "assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")
        self.cmd(oid, "start_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd(oid, "complete_task", {"task_id": "task-1"}, "art-su-1")
        self.cmd(oid, "advance_stage", {"to_stage": "cutting"}, "mgr-qin")
        self.cmd(oid, "assign_task", {"craft": "cut", "assignee": "art-tailor-1"}, "mgr-qin")
        self.cmd(oid, "start_task", {"task_id": "task-2"}, "art-tailor-1")
        self.cmd(oid, "complete_task", {"task_id": "task-2"}, "art-tailor-1")
        self.cmd(oid, "advance_stage", {"to_stage": "sewing"}, "mgr-qin")
        self.cmd(oid, "assign_task", {"craft": "tailor", "assignee": "art-tailor-2"},
                 "mgr-qin")
        self.cmd(oid, "start_task", {"task_id": "task-3"}, "art-tailor-2")
        self.cmd(oid, "complete_task", {"task_id": "task-3"}, "art-tailor-2")

        # 试衣当日顾客撤回社交平台传播许可（制作与交付不受影响）
        self.cmd(oid, "schedule_fitting",
                 {"scheduled_at": "2026-12-03T14:00:00+00:00",
                  "store_id": "store-soho"}, "adv-soho")
        self.cmd(oid, "withdraw_consent", {"scope": "social_media"}, "cust-lin")
        self.cmd(oid, "mark_fitting_done", {"fitting_id": "fit-1"}, "adv-soho")

        # 实际 12-08 交付，晚于再确认承诺的 12-05
        self.clock.set(year=2026, month=12, day=8)
        status, _ = self.cmd(oid, "deliver", {}, "mgr-qin")
        self.assertEqual(status, 200)

        status, record = self.req("GET", f"/orders/{oid}/garment", "adv-soho")
        self.assertEqual(status, 200)

        # 延期原因可从成衣记录读出（材料报废）
        self.assertTrue(record["delayed"])
        self.assertTrue(any("色差与跳丝" in r for r in record["delay_reasons"]))

        # 确认人链：顾客最初确认 + 报废后再确认，均为 cust-lin，且交期已更新
        confirmers = [(c["seq"], c["by"], c["promised_date"]) for c in
                      record["confirmer_chain"]]
        self.assertEqual(confirmers[0], (1, "cust-lin", "2026-11-20"))
        self.assertEqual(confirmers[1], (2, "cust-lin", "2026-12-05"))

        # 获准公开的故事范围：门店展示仍在，社交平台已撤回
        self.assertEqual(record["story"]["public_scopes"], ["store_showcase"])
        self.assertEqual(record["story"]["withdrawn_scopes"], ["social_media"])
        # 仍有一项许可，文化含义可公开
        self.assertIn("家族祝愿", record["story"]["cultural_meaning"])

        # 已收款与最终价格（原 1300 收款不动，附加 260 形成尾款）
        self.assertEqual(record["total_paid"], 1300)
        self.assertEqual(record["final_price"], 2860)

    # ========== 验收三：绣娘接口最小信息 ==========
    def test_artisan_endpoint_exposes_minimum_info_only(self):
        oid = "Q-MIN"
        self._build_confirmed_order(oid)
        self.cmd(oid, "prepare_material",
                 {"batch_id": "B-2", "fabric": "素绉缎", "length_m": 4}, "mgr-qin")
        self.cmd(oid, "assign_task", {"craft": "suxiu", "assignee": "art-su-1"}, "mgr-qin")

        status, view = self.view(oid, "art-su-1")
        self.assertEqual(status, 200)
        self.assertEqual(view["kind"], "artisan")
        task = view["my_tasks"][0]
        self.assertIn("motif_elements", task)
        # 顾客故事、报价、收款、量体尺寸都不应出现在绣娘视图
        blob = json.dumps(view, ensure_ascii=False)
        for secret in ("缠枝莲", "2600", "bust", "cultural_meaning"):
            self.assertNotIn(secret, blob)

    def test_health_contract_remains(self):
        status, payload = self.req("GET", "/health", None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "qipao-workflow")


if __name__ == "__main__":
    unittest.main()
