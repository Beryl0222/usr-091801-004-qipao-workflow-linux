"""首批上线验收：两家门店同时修改同一订单的冲突演练。

场景 A（领域服务层）：梅费尔与考文特花园两家门店顾问基于同一版本并行提交，
        先到者成功，后到者拿到 ConflictError（版本过期），重读后可用新版本重试。
场景 B（HTTP 层）：两个并发 POST 携带相同 expected_version，恰一者 200、
        另一者 409，且 409 响应体给出 current_version。
场景 C：两店连续编辑（串行、各自刷新版本）互不影响，证明冲突不是靠全局锁硬堵。
"""

import json
import os
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qipao import FixedClock
from qipao.errors import ConflictError
from qipao.http_api import build_service as build_http_service, make_handler
from tests.helpers import V, build_service, open_order


class InProcessConcurrencyTest(unittest.TestCase):
    def test_two_stores_same_version_one_wins_one_conflicts(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        base_version = v.v
        outcomes = []
        barrier = threading.Barrier(2)

        def mayfair_records_payment():
            barrier.wait()
            try:
                svc.record_payment(oid, "p_grace", 500, base_version,
                                   note="梅费尔门店补收尾款")
                outcomes.append(("mayfair", "ok"))
            except ConflictError:
                outcomes.append(("mayfair", "conflict"))

        def covent_marks_remeasure():
            barrier.wait()
            try:
                svc.add_measurement(
                    oid, "p_olivia",
                    {"chest": 88, "waist": 70, "hip": 92, "length": 120,
                     "shoulder": 38},
                    base_version, remeasure_reason="考文特门店复测")
                outcomes.append(("covent", "ok"))
            except ConflictError:
                outcomes.append(("covent", "conflict"))

        threads = [threading.Thread(target=mayfair_records_payment),
                   threading.Thread(target=covent_marks_remeasure)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(outcomes), 2)
        statuses = {store: result for store, result in outcomes}
        self.assertEqual(sorted(statuses.values()), ["conflict", "ok"])
        # 只成功了一次写：版本恰好前进 1
        self.assertEqual(svc.get(oid).version, base_version + 1)

        # 败者重读最新版本后重试，成功且版本继续前进
        loser = "p_olivia" if statuses["covent"] == "conflict" else "p_grace"
        fresh = svc.get(oid).version
        if loser == "p_olivia":
            _, new_version = svc.add_measurement(
                oid, "p_olivia",
                {"chest": 88, "waist": 70, "hip": 92, "length": 120,
                 "shoulder": 38}, fresh, remeasure_reason="考文特门店复测重试")
        else:
            _, new_version = svc.record_payment(
                oid, "p_grace", 500, fresh, note="梅费尔门店重试")
        self.assertEqual(new_version, fresh + 1)

    def test_serial_edits_from_both_stores_both_succeed(self):
        svc, _ = build_service()
        v = V(0)
        oid = open_order(svc, v)
        _, v.v = svc.record_payment(oid, "p_grace", 100, v.v, note="梅费尔收款")
        _, v.v = svc.record_payment(oid, "p_olivia", 200, v.v, note="考文特收款")
        totals = svc.get(oid).ledger_totals()
        self.assertEqual(totals["payments"], 1100 + 100 + 200)


class HttpConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = build_http_service(FixedClock("2026-09-21T09:00:00Z"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         make_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, payload):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode()
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        return resp.status, json.loads(raw)

    def test_http_parallel_commands_return_200_and_409(self):
        # 用 HTTP 建单并推进到制作阶段
        status, body = self._post("/orders", {
            "customer_id": "p_priya", "store_id": "store_covent",
            "actor": "p_olivia"})
        self.assertEqual(status, 201)
        oid = body["order_id"]
        version = body["version"]

        def cmd(path, payload):
            payload["expected_version"] = version
            return self._post(path, payload)

        # 两家门店同时挂起在同一版本，分别提交不同命令
        results = []
        barrier = threading.Barrier(2)

        def store_a():
            barrier.wait()
            results.append(("a",) + cmd(
                f"/orders/{oid}/commands/record_payment",
                {"actor": "p_grace", "amount": 250}))

        def store_b():
            barrier.wait()
            results.append(("b",) + cmd(
                f"/orders/{oid}/commands/add_store",
                {"actor": "p_olivia", "store_id": "store_mayfair"}))

        threads = [threading.Thread(target=store_a),
                   threading.Thread(target=store_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(r[1] for r in results)
        self.assertEqual(codes, [200, 409])
        conflict = next(r for r in results if r[1] == 409)
        self.assertEqual(conflict[2]["error"], "conflict")
        self.assertEqual(conflict[2]["details"]["expected_version"], version)
        self.assertEqual(conflict[2]["details"]["current_version"], version + 1)
        # 败者携带新版本重试，成功
        winner = next(r for r in results if r[1] == 200)
        new_version = winner[2]["version"]
        loser_payload_name = "record_payment" if conflict[0] == "a" else "add_store"
        loser_payload = ({"actor": "p_grace", "amount": 250}
                         if loser_payload_name == "record_payment"
                         else {"actor": "p_olivia", "store_id": "store_mayfair"})
        status2, body2 = self._post(
            f"/orders/{oid}/commands/{loser_payload_name}",
            {**loser_payload, "expected_version": new_version})
        self.assertEqual(status2, 200)
        self.assertEqual(body2["version"], new_version + 1)

    def test_stale_version_on_same_command_is_rejected(self):
        status, body = self._post("/orders", {
            "customer_id": "p_priya", "store_id": "store_covent",
            "actor": "p_olivia"})
        oid = body["order_id"]
        # 版本 0 上连续两次同命令
        first = self._post(f"/orders/{oid}/commands/add_store",
                           {"actor": "p_olivia", "store_id": "store_mayfair",
                            "expected_version": 0})
        self.assertEqual(first[0], 200)
        second = self._post(f"/orders/{oid}/commands/add_store",
                            {"actor": "p_olivia", "store_id": "store_mayfair",
                             "expected_version": 0})
        self.assertEqual(second[0], 409)


if __name__ == "__main__":
    unittest.main()
