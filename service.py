"""跨文化旗袍定制流转的运行入口。

- python3 service.py --check           检查服务身份与领域装配
- python3 service.py --port 8000       启动 HTTP 服务（含样例门店/人员/工艺目录）
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from domain.app import Application
from domain.httpapi import make_handler
from domain.samples import build_directory

SERVICE_ID = "qipao-workflow"
SERVICE_NAME = "跨文化旗袍定制流转"


def health_payload():
    """构造健康检查数据。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_app():
    """装配应用：样例目录 + 内存事件存储。"""
    return Application(build_directory())


# 向后兼容：旧契约测试 from service import Handler
Handler = make_handler(build_app())


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        app = build_app()
        directory = app.directory_public()
        assert len(directory["stores"]) == 2, "应装配两家伦敦门店"
        assert {"suxiu", "xiangxiu"} <= set(directory["crafts"]), "应包含苏绣与湘绣工艺"
        print("基础检查通过：两家门店、苏绣/湘绣工艺与角色目录已装配")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
