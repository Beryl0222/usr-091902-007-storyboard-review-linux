"""连环画创作审稿服务入口。

在原有健康检查之上提供版本互引、分科签署、联合会审、授权门禁与批量导出的 JSON 接口。
默认内存存储；--data 指定 JSON 快照文件可持久化（原子写入，供联调重启续用）。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from review_system import DomainError, ReviewSystem

SERVICE_ID = "storyboard-review"
SERVICE_NAME = "连环画创作审稿"

SYSTEM = None


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def get_system():
    global SYSTEM
    if SYSTEM is None:
        SYSTEM = ReviewSystem(os.environ.get("REVIEW_DATA"))
    return SYSTEM


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与审稿领域接口。"""

    # ------------------------------------------------------------ 基础框架

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON", status=400, code="bad_json")
        if not isinstance(payload, dict):
            raise DomainError("请求体必须是 JSON 对象", status=400, code="bad_json")
        return payload

    def _query(self):
        return {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send_json(health_payload())
                return
            if path.startswith("/projects/"):
                rest = path.split("/")[2:]
                query = self._query()
                if len(rest) == 2 and rest[1] == "snapshot":
                    self._send_json(get_system().snapshot(rest[0]))
                    return
                if len(rest) == 2 and rest[1] == "export":
                    scope = query.get("scope")
                    if not scope:
                        raise DomainError("导出必须指定 scope（出版范围）", 400, "missing_scope")
                    self._send_json(
                        get_system().export(rest[0], scope, as_of=query.get("as_of"))
                    )
                    return
            if path.startswith("/pages/"):
                parts = path.split("/")[1:]
                # /pages/{id}/deliverability 或 /pages/{id}/panels/{panel}/trace
                query = self._query()
                if len(parts) == 3 and parts[2] == "deliverability":
                    self._send_json(
                        get_system().deliverability(
                            parts[1], as_of=query.get("as_of"), scope=query.get("scope")
                        )
                    )
                    return
                if len(parts) == 5 and parts[2] == "panels" and parts[4] == "trace":
                    self._send_json(get_system().trace_panel(parts[1], parts[3]))
                    return
            self.send_error(404)
        except DomainError as error:
            self._send_json({"error": error.code, "message": str(error)}, error.status)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_body()
            system = get_system()
            actor = body.pop("_actor", None)

            if path == "/users":
                result = system.create_user(
                    body["name"], body.get("roles"), body.get("disciplines")
                )
            elif path == "/projects":
                result = system.create_project(body["name"], actor=actor)
            elif path == "/materials":
                result = system.create_material(
                    body["project_id"], body["name"], body["license_scopes"],
                    body["starts_at"], body["expires_at"],
                    confidential_until=body.get("confidential_until"), actor=actor,
                )
            elif path == "/artifacts":
                kind = body.pop("kind")
                result = system.create_artifact(
                    kind, body["project_id"], body["payload"], body["created_by"],
                    page_no=body.get("page_no"), name=body.get("name"),
                    expected_version=body.get("expected_version"),
                    addresses=body.get("addresses"),
                )
            elif path.startswith("/artifacts/"):
                parts = path.split("/")
                # /artifacts/{kind}/{id}/revisions
                if len(parts) == 5 and parts[4] == "revisions":
                    kind, artifact_id = parts[2], parts[3]
                    result = system.add_revision(
                        kind, artifact_id, body["payload"], body["created_by"],
                        body["expected_version"], addresses=body.get("addresses"),
                    )
                else:
                    raise DomainError("未知接口", 404, "not_found")
            elif path == "/comments":
                result = system.create_comment(
                    body["author_id"], body["discipline"], body["target_revision_id"],
                    body["summary"], severity=body.get("severity", "normal"),
                    body=body.get("body"),
                )
            elif path.startswith("/comments/"):
                parts = path.split("/")
                if len(parts) != 3:
                    raise DomainError("未知接口", 404, "not_found")
                cid = parts[2]
                action = body.get("action")
                if action == "sign":
                    result = system.sign_comment(cid, body["user_id"])
                elif action == "adopt":
                    result = system.adopt_comment(cid, body["user_id"])
                elif action == "withdraw":
                    result = system.withdraw_comment(cid, body["user_id"], body.get("note"))
                elif action == "verify":
                    result = system.verify_comment(cid, body["user_id"], body.get("note"))
                else:
                    raise DomainError("未知意见动作", 400, "bad_action")
            elif path == "/reviews/conflicts":
                result = system.raise_conflict(
                    body["project_id"], body["comment_ids"], body["reason"], body["raised_by"]
                )
            elif path == "/reviews/objections":
                result = system.raise_objection(
                    body["comment_id"], body["user_id"], body["rationale"],
                    body.get("evidence_revision_ids"),
                )
            elif path.startswith("/reviews/") and path.endswith("/ruling"):
                review_id = path.split("/")[2]
                result = system.rule_review(
                    review_id, body["decided_by"], body["decisions"], body["rationale"]
                )
            elif path.startswith("/pages/") and path.endswith("/advance"):
                page_id = path.split("/")[2]
                result = system.advance_page(page_id, body["user_id"], body["stage"])
            else:
                self.send_error(404)
                return
            self._send_json(result, 201 if path in ("/users", "/projects", "/materials",
                                                    "/artifacts", "/comments",
                                                    "/reviews/conflicts",
                                                    "/reviews/objections") else 200)
        except KeyError as error:
            self._send_json({"error": "missing_field", "message": f"缺少字段：{error.args[0]}"}, 400)
        except DomainError as error:
            self._send_json({"error": error.code, "message": str(error)}, error.status)

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_body()
            if path.startswith("/comments/") and len(path.split("/")) == 3:
                result = get_system().delete_comment(path.split("/")[2], body["user_id"])
                self._send_json(result)
                return
            self.send_error(404)
        except KeyError as error:
            self._send_json({"error": "missing_field", "message": f"缺少字段：{error.args[0]}"}, 400)
        except DomainError as error:
            self._send_json({"error": error.code, "message": str(error)}, error.status)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=os.environ.get("REVIEW_DATA"),
                        help="JSON 快照持久化路径；缺省为纯内存")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        get_system()
        print("基础检查通过")
        return
    global SYSTEM
    SYSTEM = ReviewSystem(args.data)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
