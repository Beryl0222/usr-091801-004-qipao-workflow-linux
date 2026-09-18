"""HTTP 边界：JSON 路由、X-Actor 鉴权、X-Expected-Version 乐观并发。

路由：
  GET  /health
  GET  /directory
  POST /orders                         建单（body 可带 order_id）
  GET  /orders                         门店看板（按角色过滤）
  GET  /orders/{id}                    订单视图（绣娘视图自动最小化）
  POST /orders/{id}/commands/{cmd}     执行命令
  GET  /orders/{id}/predict            交期预测
  GET  /orders/{id}/manifest           制作版本哈希核查
  GET  /orders/{id}/garment            成衣记录
"""

import json
import re
from http.server import BaseHTTPRequestHandler

from .errors import DomainError
from .timeutil import to_iso, now_utc

_ORDER_CMD = re.compile(r"^/orders/([^/]+)/commands/([a-z_]+)$")
_ORDER_GET = re.compile(r"^/orders/([^/]+)(/(predict|manifest|garment))?$")


def make_handler(app):
    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------- 基础收发 ----------
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                from .errors import ValidationFailed
                raise ValidationFailed("请求体不是合法 JSON")
            if not isinstance(data, dict):
                from .errors import ValidationFailed
                raise ValidationFailed("请求体必须是 JSON 对象")
            return data

        def _actor(self):
            return self.headers.get("X-Actor", "").strip() or None

        def _expected_version(self):
            value = self.headers.get("X-Expected-Version")
            return int(value) if value is not None else None

        def _domain(self, fn):
            try:
                return fn()
            except DomainError as err:
                self._send(err.status, err.to_body())
            except (KeyError, TypeError) as err:
                self._send(422, {"error": "validation_failed", "message": f"参数缺失或类型错误：{err}"})

        # ---------- GET ----------
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/health":
                from service import health_payload
                self._send(200, health_payload())
                return
            if path == "/directory":
                return self._domain(lambda: self._send(200, app.directory_public()))
            if path == "/orders":
                actor = self._actor()
                return self._domain(lambda: self._send(200, {"orders": app.board(actor)}))

            g = _ORDER_GET.match(path)
            if not g:
                self.send_error(404)
                return
            order_id = g.group(1)
            sub = g.group(3)
            actor = self._actor()
            if sub is None:
                return self._domain(lambda: self._send(200, app.view(order_id, actor)))
            if sub == "predict":
                return self._domain(lambda: self._send(200, app.predict(order_id, actor)))
            if sub == "manifest":
                return self._domain(lambda: self._send(200, app.manifest(order_id, actor)))
            if sub == "garment":
                return self._domain(lambda: self._send(200, app.garment(order_id, actor)))
            self.send_error(404)

        # ---------- POST ----------
        def do_POST(self):
            path = self.path.split("?")[0]

            def run():
                payload = self._read_json()
                if path == "/orders":
                    order_id = payload.pop("order_id", None) or f"Q-{to_iso(now_utc())[:10]}-{len(app.repo.list_orders()) + 1:03d}"
                    view = app.create_order(order_id, payload, self._actor())
                    self._send(201, {"order_id": order_id, "version": view["version"],
                                     "stage": view["stage"]})
                    return
                m = _ORDER_CMD.match(path)
                if not m:
                    self.send_error(404)
                    return
                order_id, cmd = m.group(1), m.group(2)
                result = app.command(order_id, cmd, payload, self._actor(),
                                     self._expected_version())
                self._send(200, result)

            self._domain(run)

        def log_message(self, *_args):
            return

    return ApiHandler
