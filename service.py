"""连环画创作审稿的运行入口与 HTTP 接口。

GET  /health          健康检查
POST /api/<action>    领域操作, JSON 请求与响应; 可用 action 见 build_actions()。
"""

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import (
    DomainError,
    License,
    NotFoundError,
    PermissionDenied,
    ReviewSystem,
    StaleVersionError,
)

SERVICE_ID = "storyboard-review"
SERVICE_NAME = "连环画创作审稿"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _jsonable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _parse_date(value):
    if value in (None, ""):
        return None
    return date.fromisoformat(value)


def _parse_license(payload):
    if payload is None:
        raise DomainError("缺少 license 字段")
    return License(
        publication_scopes=frozenset(payload.get("publication_scopes") or []),
        expires_at=_parse_date(payload.get("expires_at")),
        confidential_until=_parse_date(payload.get("confidential_until")),
    )


def build_actions(system: ReviewSystem):
    """把领域操作映射为可远程调用的 action 表。"""

    def call(method, **bound):
        def action(payload):
            kwargs = {key: payload.get(key) for key in payload}
            kwargs.update(bound)
            return _jsonable(method(**kwargs))
        return action

    def with_license(method):
        def action(payload):
            data = dict(payload)
            data["license"] = _parse_license(data.get("license"))
            return _jsonable(method(**data))
        return action

    return {
        "register_source": with_license(system.register_source),
        "revise_source": with_license(system.revise_source),
        "create_segment": call(system.create_segment),
        "new_segment_version": call(system.new_segment_version),
        "create_design": call(system.create_design),
        "new_design_version": call(system.new_design_version),
        "create_page": call(system.create_page),
        "new_page_version": call(system.new_page_version),
        "add_panel": call(system.add_panel),
        "sign_opinion": call(system.sign_opinion),
        "adopt_opinion": call(system.adopt_opinion),
        "reject_opinion": call(system.reject_opinion),
        "withdraw_opinion": call(system.withdraw_opinion),
        "delete_opinion": call(system.delete_opinion),
        "close_issue": call(system.close_issue),
        "submit_objection": call(system.submit_objection),
        "transition_page": call(system.transition_page),
        "page_blockers": call(system.page_blockers),
        "panel_trace": call(system.panel_trace),
        "export_batch": call(system.export_batch),
    }


ERROR_STATUS = {
    NotFoundError: 404,
    PermissionDenied: 403,
    StaleVersionError: 409,
}


def make_handler(system: ReviewSystem):
    actions = build_actions(system)

    class Handler(BaseHTTPRequestHandler):
        """健康检查与领域操作入口。"""

        def _send_json(self, status, payload):
            body = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            self._send_json(200, health_payload())

        def do_POST(self):
            if not self.path.startswith("/api/"):
                self.send_error(404)
                return
            action = actions.get(self.path[len("/api/"):])
            if action is None:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                self._send_json(200, action(payload))
            except DomainError as error:
                status = next(
                    (code for kind, code in ERROR_STATUS.items()
                     if isinstance(error, kind)),
                    400,
                )
                self._send_json(status, {
                    "error": str(error), "kind": type(error).__name__,
                })
            except (json.JSONDecodeError, TypeError) as error:
                self._send_json(400, {"error": f"请求格式错误: {error}", "kind": "BadRequest"})

        def log_message(self, *_args):
            return

    return Handler


SYSTEM = ReviewSystem()
Handler = make_handler(SYSTEM)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        smoke = ReviewSystem()
        assert "sign_opinion" in build_actions(smoke)
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
