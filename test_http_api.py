"""HTTP 接口端到端测试：验证 JSON API 上的完整协作场景与错误映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import service
from review_system import ReviewSystem


def request_json(base_url, method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        base_url + path, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.SYSTEM = ReviewSystem(clock=lambda: "2026-09-22")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.post = lambda path, payload: request_json(self.base_url, "POST", path, payload)
        self.get = lambda path: request_json(self.base_url, "GET", path)
        self.delete = lambda path, payload: request_json(self.base_url, "DELETE", path, payload)

    def _seed_people_and_project(self):
        users = {}
        for name, roles in (
            ("学员", ["学员"]), ("军史专家", ["军史专家"]), ("作家", ["作家"]),
            ("画家", ["画家"]), ("编辑", ["编辑"]),
        ):
            status, body = self.post("/users", {"name": name, "roles": roles})
            self.assertEqual(status, 201)
            users[name] = body["id"]
        status, project = self.post("/projects", {"name": "API 连环画"})
        self.assertEqual(status, 201)
        return users, project["id"]

    def test_full_workflow_over_http(self):
        users, pid = self._seed_people_and_project()

        # 脚本 v1。
        status, script = self.post("/artifacts", {
            "kind": "script", "project_id": pid, "created_by": users["作家"],
            "payload": {"paragraphs": [{"id": "par1", "text": "渡江。"}]},
        })
        self.assertEqual(status, 201)
        # 史料、人物设定。
        status, record = self.post("/artifacts", {
            "kind": "record", "project_id": pid, "created_by": users["军史专家"],
            "payload": {"title": "战史", "source": "档案", "confidential_until": None},
        })
        self.assertEqual(status, 201)
        status, character = self.post("/artifacts", {
            "kind": "character", "project_id": pid, "created_by": users["画家"],
            "payload": {"name": "连长"},
        })
        self.assertEqual(status, 201)
        # 素材授权（纸质）。
        status, material = self.post("/materials", {
            "project_id": pid, "name": "历史照片",
            "license_scopes": ["纸质"], "starts_at": "2020-01-01", "expires_at": "2030-01-01",
        })
        self.assertEqual(status, 201)
        status, sketch = self.post("/artifacts", {
            "kind": "sketch", "project_id": pid, "created_by": users["画家"],
            "payload": {"caption": "草图", "material_ids": [material["id"]]},
        })
        self.assertEqual(status, 201)

        page_payload = {
            "page_no": 1,
            "script_version": script["revisions"][-1]["rev_id"],
            "record_versions": {record["id"]: record["revisions"][-1]["rev_id"]},
            "design_versions": {character["id"]: character["revisions"][-1]["rev_id"]},
            "sketch_versions": {sketch["id"]: sketch["revisions"][-1]["rev_id"]},
            "panels": [{
                "panel_id": "frame1", "paragraph_id": "par1",
                "record_id": record["id"], "character_id": character["id"],
                "sketch_id": sketch["id"],
            }],
        }
        status, page = self.post("/artifacts", {
            "kind": "page", "project_id": pid, "created_by": users["学员"],
            "page_no": 1, "name": "第1页", "payload": page_payload,
        })
        self.assertEqual(status, 201)
        page_id = page["id"]

        # 军史专家提重大事实意见，画家跨学科签署被拒（403）。
        status, comment = self.post("/comments", {
            "author_id": users["军史专家"], "discipline": "军史",
            "target_revision_id": page["revisions"][-1]["rev_id"],
            "summary": "时间线错误", "severity": "major",
        })
        self.assertEqual(status, 201)
        status, body = self.post(f"/comments/{comment['id']}",
                                 {"action": "sign", "user_id": users["画家"]})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "out_of_scope")
        self.post(f"/comments/{comment['id']}",
                  {"action": "sign", "user_id": users["军史专家"]})
        self.post(f"/comments/{comment['id']}",
                  {"action": "adopt", "user_id": users["编辑"]})

        # 门禁：重大事实未关闭不得转精稿。
        self.post(f"/pages/{page_id}/advance", {"user_id": users["编辑"], "stage": "待评审"})
        status, body = self.post(f"/pages/{page_id}/advance",
                                 {"user_id": users["编辑"], "stage": "精稿中"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "gate_fact_open")

        # 学员改稿（基于 v1），专家核验关闭，流程走完。
        status, body = self.post(f"/artifacts/page/{page_id}/revisions", {
            "created_by": users["学员"], "expected_version": 1,
            "payload": page_payload, "addresses": [comment["id"]],
        })
        self.assertEqual(status, 200)
        self.post(f"/comments/{comment['id']}",
                  {"action": "verify", "user_id": users["军史专家"]})
        for stage in ("精稿中", "可出版"):
            status, body = self.post(f"/pages/{page_id}/advance",
                                     {"user_id": users["编辑"], "stage": stage})
            self.assertEqual(status, 200, body)

        # 反查画格：史料版本、文字、决定人齐备。
        status, trace = self.get(f"/pages/{page_id}/panels/frame1/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["script"]["text"], "渡江。")
        self.assertTrue(trace["comment_decisions"])

        # 电子范围未授权：导出为空且说明原因。
        status, bundle = self.get(f"/projects/{pid}/export?scope={quote('电子')}")
        self.assertEqual(status, 200)
        self.assertEqual(bundle["included"], [])
        # 纸质范围：该页入选。
        status, bundle = self.get(f"/projects/{pid}/export?scope={quote('纸质')}")
        self.assertEqual([p["page_id"] for p in bundle["included"]], [page_id])

        # 并发改稿：作家先提交 v2，另一人仍基于 v1 提交则冲突。
        status, _ = self.post(f"/artifacts/script/{script['id']}/revisions", {
            "created_by": users["作家"], "expected_version": 1,
            "payload": {"paragraphs": [{"id": "par1", "text": "新句"}]},
        })
        self.assertEqual(status, 200)
        status, body = self.post(f"/artifacts/script/{script['id']}/revisions", {
            "created_by": users["作家"], "expected_version": 1,
            "payload": {"paragraphs": [{"id": "par1", "text": "旧版改法"}]},
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "version_conflict")

    def test_withdrawn_comment_is_protected_over_http(self):
        users, pid = self._seed_people_and_project()
        _, script = self.post("/artifacts", {
            "kind": "script", "project_id": pid, "created_by": users["作家"],
            "payload": {"paragraphs": [{"id": "par1", "text": "文"}]},
        })
        _, comment = self.post("/comments", {
            "author_id": users["作家"], "discipline": "文学",
            "target_revision_id": script["revisions"][-1]["rev_id"],
            "summary": "用词存疑",
        })
        self.post(f"/comments/{comment['id']}",
                  {"action": "sign", "user_id": users["作家"]})
        self.post(f"/comments/{comment['id']}",
                  {"action": "adopt", "user_id": users["编辑"]})
        self.post(f"/comments/{comment['id']}",
                  {"action": "withdraw", "user_id": users["作家"], "note": "复核后撤"})
        status, body = self.delete(f"/comments/{comment['id']}",
                                   {"user_id": users["编辑"]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "comment_protected")
        # 事件日志完整保留（含删除被拒前的全部动作）。
        _, snapshot = self.get(f"/projects/{pid}/snapshot")
        self.assertIn(comment["id"], snapshot["comments"])
        self.assertEqual(snapshot["comments"][comment["id"]]["status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
