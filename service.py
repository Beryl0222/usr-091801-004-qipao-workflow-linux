"""服务运行入口：复用 qipao 包，保留基线 --check/--port 与 /health 契约。"""

import argparse
from http.server import ThreadingHTTPServer

from qipao.http_api import (
    SERVICE_ID,
    SERVICE_NAME,
    build_service,
    health_payload,
    make_handler,
)

# 基线契约（service_contract.py）按模块级名字导入
service = build_service()
Handler = make_handler(service)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
