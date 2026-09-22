"""连环画审稿领域规则的端到端测试。

用固定时钟模拟授权有效期/保密期，覆盖：
版本互引与陈旧依赖、分科签署权限、冲突会审与学员异议、
重大事实门禁、采纳后撤回不可删、授权/保密/出版范围、画格反查与批量导出、并发改稿。
"""

import threading
import unittest

from review_system import DomainError, ReviewSystem


class ReviewDomainTest(unittest.TestCase):
    def setUp(self):
        self.day = "2026-09-22"
        self.system = ReviewSystem(clock=lambda: self.day)
        self.student = self.system.create_user("学员小李", roles=["学员"])
        self.historian = self.system.create_user("王党史", roles=["军史专家"])
        self.writer = self.system.create_user("张作家", roles=["作家"])
        self.painter = self.system.create_user("刘画家", roles=["画家"])
        self.editor = self.system.create_user("陈编辑", roles=["编辑"])
        self.historian2 = self.system.create_user("赵党史", roles=["军史专家"])
        self.project = self.system.create_project("湘江战役连环画", actor=self.editor["id"])
        self.pid = self.project["id"]

    def _script(self, paragraphs):
        return self.system.create_artifact(
            "script", self.pid, {"paragraphs": paragraphs}, self.writer["id"], name="文字脚本"
        )

    def _record(self, title, confidential_until=None):
        return self.system.create_artifact(
            "record", self.pid,
            {"title": title, "source": f"《{title}》", "confidential_until": confidential_until},
            self.historian["id"], name=title,
        )

    def _character(self, name):
        return self.system.create_artifact(
            "character", self.pid, {"name": name, "look": f"{name}造型v1"},
            self.painter["id"], name=name,
        )

    def _material(self, name, scopes, expires_at, starts_at="2020-01-01", until=None):
        return self.system.create_material(
            self.pid, name, scopes, starts_at, expires_at, confidential_until=until,
            actor=self.editor["id"],
        )

    def _sketch(self, material_ids):
        return self.system.create_artifact(
            "sketch", self.pid, {"caption": "草图", "material_ids": material_ids},
            self.painter["id"], name="草图",
        )

    def _page_payload(self, script_art, record_art, char_art, sketch_art=None):
        script_rev = script_art["revisions"][-1]["rev_id"]
        records = {record_art["id"]: record_art["revisions"][-1]["rev_id"]}
        designs = {char_art["id"]: char_art["revisions"][-1]["rev_id"]}
        sketches = {}
        if sketch_art:
            sketches[sketch_art["id"]] = sketch_art["revisions"][-1]["rev_id"]
        return {
            "page_no": 1,
            "script_version": script_rev,
            "record_versions": records,
            "design_versions": designs,
            "sketch_versions": sketches,
            "panels": [
                {
                    "panel_id": "p1",
                    "paragraph_id": "para-1",
                    "record_id": record_art["id"],
                    "character_id": char_art["id"],
                    "sketch_id": sketch_art["id"] if sketch_art else None,
                }
            ],
        }

    def _page(self, payload):
        return self.system.create_artifact(
            "page", self.pid, payload, self.student["id"], page_no=payload["page_no"], name="第1页"
        )

    def _full_page(self, confidential_until=None, material_expires="2030-01-01",
                   scopes=("纸质", "电子")):
        script = self._script([{"id": "para-1", "text": "红军强渡湘江。"}])
        record = self._record("湘江战役文献", confidential_until)
        char = self._character("团长")
        material = self._material("战地照片", list(scopes), material_expires)
        sketch = self._sketch([material["id"]])
        page = self._page(self._page_payload(script, record, char, sketch))
        return page, script, record, char, material, sketch

    # ------------------------------------------------------------ 版本互引

    def test_page_must_pin_existing_script_paragraph(self):
        record = self._record("文献")
        char = self._character("团长")
        with self.assertRaises(DomainError) as error:
            self._page(
                {
                    "page_no": 1,
                    "script_version": "script:not-exist#v1",
                    "record_versions": {},
                    "design_versions": {},
                    "sketch_versions": {},
                    "panels": [],
                }
            )
        self.assertEqual(error.exception.code, "bad_reference")

        script = self._script([{"id": "para-1", "text": "文字"}])
        payload = self._page_payload(script, record, char)
        payload["panels"][0]["paragraph_id"] = "para-x"
        with self.assertRaises(DomainError) as error:
            self._page(payload)
        self.assertEqual(error.exception.code, "bad_reference")

    def test_concurrent_revision_conflict(self):
        script = self._script([{"id": "para-1", "text": "旧句"}])
        self.system.add_revision(
            "script", script["id"],
            {"paragraphs": [{"id": "para-1", "text": "甲的改法"}]},
            self.writer["id"], expected_version=1,
        )
        # 乙仍基于 v1 提交，必须被告知冲突而不是静默覆盖。
        with self.assertRaises(DomainError) as error:
            self.system.add_revision(
                "script", script["id"],
                {"paragraphs": [{"id": "para-1", "text": "乙的改法"}]},
                self.writer["id"], expected_version=1,
            )
        self.assertEqual(error.exception.code, "version_conflict")
        # 乙基于最新版重新提交成功。
        self.system.add_revision(
            "script", script["id"],
            {"paragraphs": [{"id": "para-1", "text": "乙合并后的改法"}]},
            self.writer["id"], expected_version=2,
        )

    def test_stale_dependency_blocks_delivery(self):
        page, script, record, char, material, sketch = self._full_page()
        self.assertEqual(self.system.deliverability(page["id"])["deliverable"], True)
        # 史料出了新版而页面仍锁旧版 → 依赖陈旧，明确不可交付。
        self.system.add_revision(
            "record", record["id"],
            {"title": "湘江战役文献", "source": "新考证", "confidential_until": None},
            self.historian["id"], expected_version=1,
        )
        report = self.system.deliverability(page["id"])
        self.assertFalse(report["deliverable"])
        self.assertIn("stale_dependency", {b["code"] for b in report["blockers"]})

    # ------------------------------------------------------------ 签署权限

    def test_experts_sign_only_own_discipline(self):
        page, *_unused = self._full_page()
        rev = page["revisions"][-1]["rev_id"]
        comment = self.system.create_comment(
            self.historian["id"], "军史", rev, "渡口方位与史料不符", severity="major"
        )
        # 画家不能签署军史意见。
        with self.assertRaises(DomainError) as error:
            self.system.sign_comment(comment["id"], self.painter["id"])
        self.assertEqual(error.exception.code, "out_of_scope")
        # 作家也不能出具军史意见。
        with self.assertRaises(DomainError) as error:
            self.system.create_comment(self.writer["id"], "军史", rev, "越界")
        self.assertEqual(error.exception.code, "out_of_scope")
        # 军史专家签署、编辑采纳。
        self.system.sign_comment(comment["id"], self.historian["id"])
        self.system.adopt_comment(comment["id"], self.editor["id"])
        with self.assertRaises(DomainError):
            self.system.adopt_comment(comment["id"], self.student["id"])

    # ------------------------------------------------------------ 门禁与会审

    def test_major_fact_blocks_polish_until_closed(self):
        page, *_unused = self._full_page()
        rev = page["revisions"][-1]["rev_id"]
        comment = self.system.create_comment(
            self.historian["id"], "军史", rev, "部队番号错误", severity="major"
        )
        self.system.sign_comment(comment["id"], self.historian["id"])
        self.system.adopt_comment(comment["id"], self.editor["id"])
        # 构思中 → 待评审允许；重大事实未关闭时，待评审 → 精稿中被拦截。
        self.system.advance_page(page["id"], self.editor["id"], "待评审")
        with self.assertRaises(DomainError) as gate:
            self.system.advance_page(page["id"], self.editor["id"], "精稿中")
        self.assertEqual(gate.exception.code, "gate_fact_open")
        # 学员改稿并声称修正；军史专家核验后关闭，门禁放行。
        self.system.add_revision(
            "page", page["id"], page["revisions"][-1]["payload"],
            self.student["id"], expected_version=1, addresses=[comment["id"]],
        )
        with self.assertRaises(DomainError):
            # 学员不能自行核验关闭。
            self.system.verify_comment(comment["id"], self.student["id"])
        self.system.verify_comment(comment["id"], self.historian["id"], note="番号已订正")
        self.system.advance_page(page["id"], self.editor["id"], "精稿中")
        self.assertEqual(page["stage"], "精稿中")

    def test_conflict_review_requires_panel_and_ruling(self):
        page, script, record, char, material, sketch = self._full_page()
        page_rev = page["revisions"][-1]["rev_id"]
        hist_comment = self.system.create_comment(
            self.historian["id"], "军史", page_rev, "必须表现阻击战，史实要求"
        )
        lit_comment = self.system.create_comment(
            self.writer["id"], "文学", page_rev, "加阻击战会破坏分镜节奏"
        )
        for cid in (hist_comment["id"], lit_comment["id"]):
            self.system.sign_comment(cid, self.historian["id"] if cid == hist_comment["id"]
                                     else self.writer["id"])
        # 同一学科两条意见不构成联合会审。
        with self.assertRaises(DomainError):
            self.system.raise_conflict(
                self.pid, [hist_comment["id"], hist_comment["id"]], "重复", self.editor["id"]
            )
        review = self.system.raise_conflict(
            self.pid, [hist_comment["id"], lit_comment["id"]],
            "史实修正与分镜冲突", self.editor["id"],
        )
        # 会审未裁决 → 精稿门禁拦截。
        self.system.advance_page(page["id"], self.editor["id"], "待评审")
        with self.assertRaises(DomainError) as gate:
            self.system.advance_page(page["id"], self.editor["id"], "精稿中")
        self.assertEqual(gate.exception.code, "gate_fact_open")
        # 缺学科的裁决组不行。
        with self.assertRaises(DomainError) as error:
            self.system.rule_review(
                review["id"], [self.historian["id"]],
                {hist_comment["id"]: "uphold", lit_comment["id"]: "dismiss"}, "军史优先",
            )
        self.assertEqual(error.exception.code, "panel_incomplete")
        # 两学科会签裁决。
        self.system.rule_review(
            review["id"], [self.historian["id"], self.writer["id"]],
            {hist_comment["id"]: "uphold", lit_comment["id"]: "revise"},
            "保留阻击战并调整节奏，由文学侧重排分镜",
        )
        self.assertEqual(self.system.store["comments"][hist_comment["id"]]["status"], "adopted")
        # 会审已关闭，但裁决生效的意见仍需核验；学员改稿后双学科核验。
        self.system.add_revision(
            "page", page["id"], page["revisions"][-1]["payload"],
            self.student["id"], expected_version=1,
            addresses=[hist_comment["id"], lit_comment["id"]],
        )
        self.system.verify_comment(hist_comment["id"], self.historian["id"])
        # 重大事实已关闭即可进入精稿；但普通意见未核验仍会在交付门拦截。
        self.system.advance_page(page["id"], self.editor["id"], "精稿中")
        with self.assertRaises(DomainError) as gate:
            self.system.advance_page(page["id"], self.editor["id"], "可出版")
        self.assertEqual(gate.exception.code, "gate_not_deliverable")
        self.system.verify_comment(lit_comment["id"], self.writer["id"])
        self.system.advance_page(page["id"], self.editor["id"], "可出版")
        self.assertEqual(page["stage"], "可出版")

    def test_student_objection_with_evidence(self):
        page, *_unused = self._full_page()
        record2 = self._record("军委电报")
        rev = page["revisions"][-1]["rev_id"]
        comment = self.system.create_comment(
            self.historian["id"], "军史", rev, "该场景不可能发生", severity="major"
        )
        self.system.sign_comment(comment["id"], self.historian["id"])
        # 无依据的异议不受理。
        with self.assertRaises(DomainError) as error:
            self.system.raise_objection(comment["id"], self.student["id"], "我认为可以")
        self.assertEqual(error.exception.code, "no_evidence")
        review = self.system.raise_objection(
            comment["id"], self.student["id"], "电报证明当天部队已抵达",
            evidence_revision_ids=[record2["revisions"][-1]["rev_id"]],
        )
        self.assertEqual(review["kind"], "objection")
        self.assertEqual(self.system.store["comments"][comment["id"]]["status"], "in_review")
        # 画家不能替学员提异议。
        with self.assertRaises(DomainError):
            self.system.raise_objection(
                comment["id"], self.painter["id"], "x",
                evidence_revision_ids=[record2["revisions"][-1]["rev_id"]],
            )
        # 会审驳回异议。
        self.system.rule_review(
            review["id"], [self.historian2["id"], self.writer["id"]],
            {comment["id"]: "uphold"}, "电报时间为次日，异议不成立",
        )
        self.assertEqual(self.system.store["comments"][comment["id"]]["status"], "adopted")

    # ------------------------------------------------------------ 意见删除保护

    def test_adopted_then_withdrawn_comment_cannot_be_deleted(self):
        page, *_unused = self._full_page()
        rev = page["revisions"][-1]["rev_id"]
        comment = self.system.create_comment(self.writer["id"], "文学", rev, "台词太现代")
        self.system.sign_comment(comment["id"], self.writer["id"])
        self.system.adopt_comment(comment["id"], self.editor["id"])
        self.system.withdraw_comment(comment["id"], self.writer["id"], note="重新核对后撤回")
        with self.assertRaises(DomainError) as error:
            self.system.delete_comment(comment["id"], self.editor["id"])
        self.assertEqual(error.exception.code, "comment_protected")
        # 记录仍在、撤回人与完整历史可审计。
        kept = self.system.store["comments"][comment["id"]]
        self.assertEqual(kept["status"], "withdrawn")
        self.assertEqual(kept["withdrawn_by"], self.writer["id"])
        self.assertTrue(kept["ever_adopted"])
        actions = [item["action"] for item in kept["history"]]
        self.assertEqual(actions, ["created", "signed", "adopted", "withdrawn"])
        # 未签署的初稿意见可删。
        draft = self.system.create_comment(self.writer["id"], "文学", rev, "待定")
        self.system.delete_comment(draft["id"], self.editor["id"])
        self.assertNotIn(draft["id"], self.system.store["comments"])

    # ------------------------------------------------------------ 授权与导出

    def test_expired_license_and_scope_and_confidentiality(self):
        # 保密期未过。
        page, *_rest = self._full_page(
            confidential_until="2027-01-01", material_expires="2030-01-01"
        )
        report = self.system.deliverability(page["id"], scope="纸质")
        self.assertIn("confidentiality_active", {b["code"] for b in report["blockers"]})

        # 授权过期。
        page2, script, record, char, material, sketch = self._full_page(
            material_expires="2025-01-01"
        )
        report = self.system.deliverability(page2["id"], scope="纸质")
        self.assertIn("license_expired", {b["code"] for b in report["blockers"]})

        # 出版范围未授权（只授了纸质，请求电子）。
        page3, *_ = self._full_page(scopes=("纸质",))
        report = self.system.deliverability(page3["id"], scope="电子")
        self.assertIn("license_scope_denied", {b["code"] for b in report["blockers"]})
        self.assertFalse(report["deliverable"])
        # 时间推进后同页自动变为过期，无需改稿。
        self.day = "2031-01-01"
        report = self.system.deliverability(page3["id"], scope="纸质")
        self.assertIn("license_expired", {b["code"] for b in report["blockers"]})

    def test_export_only_contains_currently_licensed_publishable_pages(self):
        good, *_ = self._full_page()
        # 走完流程：无意见 → 待评审 → 精稿中 → 可出版。
        for stage in ("待评审", "精稿中", "可出版"):
            self.system.advance_page(good["id"], self.editor["id"], stage)

        # 第二页：授权过期，即便到了可出版也应在导出时重新求值并剔除。
        bad, script2, record2, char2, material2, sketch2 = self._full_page(
            material_expires="2025-01-01"
        )
        bad2 = bad
        # 构造时授权有效（时钟 2026），先推到可出版不可能（门禁拦截）；
        # 因此直接验证导出排除逻辑：未达可出版 + 授权问题双重原因。
        bundle = self.system.export(self.pid, scope="纸质")
        self.assertEqual([p["page_id"] for p in bundle["included"]], [good["id"]])
        excluded = {p["page_id"]: p for p in bundle["excluded"]}
        self.assertIn(bad2["id"], excluded)
        reasons = {r["code"] for r in excluded[bad2["id"]]["reasons"]}
        self.assertIn("license_expired", reasons)
        self.assertIn("stage_not_publishable", reasons)

        # 电子范围未授权时，第一页也被剔除。
        bundle = self.system.export(self.pid, scope="数字藏品")
        self.assertEqual(bundle["included"], [])
        self.assertTrue(
            any(r["code"] == "license_scope_denied"
                for p in bundle["excluded"] if p["page_id"] == good["id"]
                for r in p["reasons"])
        )

    # ------------------------------------------------------------ 反查

    def test_trace_panel_locates_sources_text_and_deciders(self):
        page, script, record, char, material, sketch = self._full_page()
        page_rev = page["revisions"][-1]["rev_id"]
        comment = self.system.create_comment(
            self.historian["id"], "军史", page_rev, "番号订正", severity="major"
        )
        self.system.sign_comment(comment["id"], self.historian["id"])
        self.system.adopt_comment(comment["id"], self.editor["id"])
        self.system.add_revision(
            "page", page["id"], page["revisions"][-1]["payload"],
            self.student["id"], expected_version=1, addresses=[comment["id"]],
        )
        self.system.verify_comment(comment["id"], self.historian["id"], note="已核实")

        trace = self.system.trace_panel(page["id"], "p1")
        self.assertEqual(trace["script"]["text"], "红军强渡湘江。")
        self.assertIn("湘江战役文献", trace["historical_record"]["title"])
        self.assertEqual(trace["historical_record"]["revision"], f"record:{record['id']}#v1")
        self.assertEqual(trace["materials"][0]["id"], material["id"])
        decision = next(d for d in trace["comment_decisions"] if d["comment_id"] == comment["id"])
        roles = {entry["role"]: entry["name"] for entry in decision["deciders"]}
        self.assertEqual(roles["签署人"], "王党史")
        self.assertEqual(roles["核验关闭人"], "王党史")
        self.assertEqual(decision["status"], "addressed")
        self.assertTrue(trace["page_revision"].endswith("#v2"))

    # ------------------------------------------------------------ 并发

    def test_concurrent_edits_are_serialized_without_corruption(self):
        script = self._script([{"id": "para-1", "text": "起"}])
        errors = []

        def worker(seed):
            try:
                # 所有参与者都基于 v1 并发提交，模拟多人同时改同一稿。
                self.system.add_revision(
                    "script", script["id"],
                    {"paragraphs": [{"id": "para-1", "text": f"改{seed}"}]},
                    self.writer["id"], expected_version=1,
                )
            except DomainError as error:
                errors.append(error.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 恰好一个线程成功，其余 18 个全部版本冲突；修订链无空洞、无覆盖。
        self.assertEqual(len(script["revisions"]), 2)
        self.assertEqual(errors.count("version_conflict"), 18)


if __name__ == "__main__":
    unittest.main()
