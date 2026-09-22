"""通过 HTTP 接口验证领域操作的端到端行为。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import ReviewSystem
from service import make_handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ReviewSystem()))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, action, payload=None):
        request = Request(
            f"{self.base_url}/api/{action}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def _seed_page(self):
        _, source = self.call("register_source", {
            "title": "战斗详报", "citation": "《战史》p12",
            "license": {"publication_scopes": ["国内", "海外"]},
            "actor": "王编辑", "role": "编辑",
        })
        _, segment = self.call("create_segment", {
            "title": "夜袭", "text": "拂晓进入阵地。", "actor": "学员甲",
        })
        _, design = self.call("create_design", {
            "name": "连长", "brief": "三十岁。", "actor": "学员甲",
        })
        _, page = self.call("create_page", {
            "title": "第3页",
            "script_refs": {segment["id"]: 1},
            "design_refs": {design["id"]: 1},
            "source_refs": {source["id"]: 1},
            "actor": "学员甲",
        })
        return source, segment, design, page

    def test_out_of_scope_signature_is_forbidden(self):
        _, _, _, page = self._seed_page()
        status, body = self.call("sign_opinion", {
            "target_kind": "page", "target_id": page["id"], "scope": "史实",
            "stance": "x", "content": "作家越权", "author": "赵作家", "role": "作家",
        })
        self.assertEqual(status, 403)
        self.assertEqual(body["kind"], "PermissionDenied")

    def test_unknown_action_and_bad_payload(self):
        request = Request(f"{self.base_url}/api/nope", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        status, body = self.call("create_segment", {"unexpected": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["kind"], "BadRequest")

    def test_review_flow_to_export_over_http(self):
        source, segment, _, page = self._seed_page()
        _, opinion = self.call("sign_opinion", {
            "target_kind": "page", "target_id": page["id"], "scope": "史实",
            "stance": "日期为9月25日", "content": "依战报",
            "author": "李专家", "role": "党史专家",
        })
        status, _ = self.call("adopt_opinion", {
            "opinion_id": opinion["id"], "actor": "王编辑", "role": "编辑",
        })
        self.assertEqual(status, 200)
        for target in ("待评审", "精稿中", "可出版"):
            status, _ = self.call("transition_page", {
                "page_id": page["id"], "target": target,
                "actor": "王编辑", "role": "编辑",
            })
            self.assertEqual(status, 200, target)
        status, result = self.call("export_batch", {
            "scope": "海外", "actor": "王编辑", "role": "编辑",
        })
        self.assertEqual(status, 200)
        self.assertEqual([p["page_id"] for p in result["pages"]], [page["id"]])
        self.assertEqual(result["pages"][0]["sources"][0]["source_id"], source["id"])

        # 史料授权过期后, 同一页面立即不可交付且被导出排除。
        status, _ = self.call("revise_source", {
            "source_id": source["id"], "citation": "《战史》p12(修订)",
            "license": {"publication_scopes": ["国内", "海外"],
                        "expires_at": "2020-01-01"},
            "actor": "王编辑", "role": "编辑", "base_revision": 1,
        })
        self.assertEqual(status, 200)
        status, blockers = self.call("page_blockers", {"page_id": page["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(any("过期" in item for item in blockers))
        _, result = self.call("export_batch", {
            "scope": "海外", "actor": "王编辑", "role": "编辑",
        })
        self.assertEqual(result["pages"], [])
        self.assertIn(page["id"], result["excluded"])

    def test_stale_version_conflict_maps_to_409(self):
        _, segment, _, _ = self._seed_page()
        self.call("new_segment_version", {
            "segment_id": segment["id"], "text": "第二版",
            "actor": "学员甲", "base_version": 1,
        })
        status, body = self.call("new_segment_version", {
            "segment_id": segment["id"], "text": "第三版",
            "actor": "学员乙", "base_version": 1,
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["kind"], "StaleVersionError")


if __name__ == "__main__":
    unittest.main()
