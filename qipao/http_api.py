"""HTTP 适配层：JSON over 标准库 http.server，无第三方依赖。

路由：
- GET  /health
- POST /orders
- GET  /orders/{id}?actor=...            按角色投影（工匠自动得到最小信息卡）
- GET  /orders/{id}/garment              成衣记录
- GET  /orders/{id}/schedule             交期预测
- POST /orders/{id}/commands/{name}      命令（body 携带 actor 与 expected_version）

所有写命令都要求 expected_version；版本过期返回 409，
响应体给出 current_version，客户端重读后可重试。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import OrderService
from .catalog import REGISTRY
from .clock import SystemClock
from .errors import DomainError
from .repository import OrderRepository
from .scheduler import Scheduler

SERVICE_ID = "qipao-workflow"
SERVICE_NAME = "跨文化旗袍定制流转"


def health_payload():
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service(clock=None):
    repo = OrderRepository()
    service = OrderService(repo, clock=clock, scheduler=Scheduler(clock or SystemClock()))
    return service


# 命令名 -> (service 方法, 必填字段)
COMMANDS = {
    "add_store": ("add_store", ["actor", "store_id"]),
    "record_story": ("record_story", ["actor", "customer", "content", "significance"]),
    "set_consent": ("set_consent", ["actor", "granted"]),
    "set_publication": ("set_publication", ["actor", "scope", "granted"]),
    "add_measurement": ("add_measurement", ["actor", "points"]),
    "submit_draft": ("submit_draft",
                     ["actor", "elements", "embroidery_style"]),
    "approve_version": ("approve_version",
                        ["actor", "draft_id", "measurement_version", "quote",
                         "fabric_batch_id"]),
    "raise_change": ("raise_change",
                     ["actor", "reason", "summary", "affected_stage",
                      "cost_delta", "extra_work_days"]),
    "amend_change": ("amend_change", ["actor", "change_id", "changes"]),
    "confirm_change": ("confirm_change", ["actor", "change_id"]),
    "reject_change": ("reject_change", ["actor", "change_id"]),
    "allocate_material": ("allocate_material", ["actor", "batch_id", "meters"]),
    "scrap_material": ("scrap_material", ["actor", "batch_id", "reason"]),
    "assign_task": ("assign_task", ["actor", "stage", "assignee"]),
    "start_task": ("start_task", ["actor", "task_id"]),
    "complete_task": ("complete_task", ["actor", "task_id"]),
    "request_rework": ("request_rework", ["actor", "task_id", "reason"]),
    "log_work": ("log_work", ["actor", "task_id", "hours", "hourly_rate"]),
    "record_payment": ("record_payment", ["actor", "amount"]),
    "schedule_fitting": ("schedule_fitting", ["actor", "when"]),
    "mark_late": ("mark_late",
                  ["actor", "fitting_id", "reschedule_at"]),
    "record_fitting": ("record_fitting", ["actor", "fitting_id", "result"]),
    "deliver": ("deliver", ["actor"]),
}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise _BadRequest(f"请求体不是合法 JSON：{exc}")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            query = self._query()
            if path == "/health":
                self._send(200, health_payload())
                return
            if path == "/orders":
                self._send(200, {"orders": service.repo.list_ids()})
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "orders":
                actor = query.get("actor")
                if not actor:
                    self._send(400, {"error": "validation_error",
                                     "message": "查询订单必须携带 actor，"
                                                "系统按角色裁剪可见信息"})
                    return
                try:
                    view, version = service.view_for(parts[1], actor)
                except DomainError as exc:
                    self._domain_error(exc)
                    return
                self._send(200, {"version": version, "view": view})
                return
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "garment":
                try:
                    self._send(200, service.garment(parts[1]))
                except DomainError as exc:
                    self._domain_error(exc)
                return
            if len(parts) == 3 and parts[0] == "orders" and parts[2] == "schedule":
                try:
                    self._send(200, service.schedule(
                        parts[1],
                        include_pending=query.get("include_pending") == "true"))
                except DomainError as exc:
                    self._domain_error(exc)
                return
            self.send_error(404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/orders":
                body = self._read_json()
                try:
                    for field_name in ("customer_id", "store_id", "actor"):
                        if not body.get(field_name):
                            raise _BadRequest(f"缺少字段：{field_name}")
                    order_id, version = service.create_order(
                        body["customer_id"], body["store_id"], body["actor"])
                except DomainError as exc:
                    self._domain_error(exc)
                    return
                self._send(201, {"order_id": order_id, "version": version})
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4 and parts[0] == "orders" and parts[2] == "commands":
                self._dispatch(parts[1], parts[3])
                return
            self.send_error(404)

        def _dispatch(self, order_id, command_name):
            spec = COMMANDS.get(command_name)
            if spec is None:
                self.send_error(404)
                return
            method_name, required = spec
            body = self._read_json()
            try:
                for field_name in required:
                    if field_name not in body:
                        raise _BadRequest(f"缺少字段：{field_name}")
                if "expected_version" not in body:
                    raise _BadRequest("缺少字段：expected_version")
                kwargs = {k: v for k, v in body.items() if k != "expected_version"}
                result, version = getattr(service, method_name)(
                    order_id, expected_version=body["expected_version"], **kwargs)
            except DomainError as exc:
                self._domain_error(exc)
                return
            payload = {"version": version}
            if isinstance(result, str):
                payload["id"] = result
            elif isinstance(result, dict):
                payload["result"] = result
            elif hasattr(result, "__dict__"):
                payload["result"] = result.__dict__
            self._send(200, payload)

        def _domain_error(self, exc):
            self._send(exc.http_status, {
                "error": exc.code, "message": exc.message, "details": exc.details,
            })

        def _query(self):
            if "?" not in self.path:
                return {}
            out = {}
            for pair in self.path.split("?", 1)[1].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    out[k] = v
            return out

        def log_message(self, *_args):
            return

    return Handler


class _BadRequest(DomainError):
    http_status = 400
    code = "validation_error"


def serve(port, service=None):
    service = service or build_service()
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(service)).serve_forever()
