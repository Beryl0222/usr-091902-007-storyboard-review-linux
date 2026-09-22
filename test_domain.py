"""连环画创作审稿的领域规则测试。"""

import unittest
from datetime import date, datetime

from domain import (
    DomainError,
    License,
    PermissionDenied,
    ReviewSystem,
    StaleVersionError,
    StateError,
)

EDITOR = {"actor": "王编辑", "role": "编辑"}
NOW = datetime(2026, 9, 22, 10, 0, 0)


def lic(scopes=("国内",), expires=None, confidential=None):
    return License(
        publication_scopes=frozenset(scopes),
        expires_at=expires,
        confidential_until=confidential,
    )


class ReviewSystemTest(unittest.TestCase):
    def setUp(self):
        self.system = ReviewSystem(clock=lambda: NOW)
        self.source = self.system.register_source(
            title="平型关战斗详报", citation="《八路军战史》第12页",
            license=lic(("国内", "海外")), **EDITOR)
        self.segment = self.system.create_segment(
            title="夜袭", text="九月二十五日拂晓, 部队进入伏击阵地。", actor="学员甲")
        self.design = self.system.create_design(
            name="连长", brief="三十岁, 左眉有疤。", actor="学员甲")
        self.page = self.system.create_page(
            title="第3页 伏击",
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲")
        self.panel = self.system.add_panel(
            self.page.id, page_version=1, index=1,
            sketch_ref="sketches/3-1.png", actor="学员甲")

    # ----- 专业签署 -----

    def test_expert_signs_only_within_own_scope(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期应为9月25日", content="与战报一致", author="李专家",
            role="党史专家")
        self.assertEqual(opinion.state, "提出")
        with self.assertRaises(PermissionDenied):
            self.system.sign_opinion(
                target_kind="page", target_id=self.page.id, scope="史实",
                stance="x", content="作家越权签史实", author="赵作家", role="作家")
        with self.assertRaises(PermissionDenied):
            self.system.sign_opinion(
                target_kind="page", target_id=self.page.id, scope="画面",
                stance="x", content="学员不能签署", author="学员甲", role="学员")

    # ----- 意见生命周期 -----

    def test_adopted_then_withdrawn_opinion_cannot_be_deleted(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="文字",
            stance="精简旁白", content="旁白过长", author="赵作家", role="作家")
        self.system.adopt_opinion(opinion.id, **EDITOR)
        self.system.withdraw_opinion(opinion.id, **EDITOR)
        self.assertEqual(self.system.opinions[opinion.id].state, "撤回")
        with self.assertRaises(PermissionDenied):
            self.system.delete_opinion(opinion.id, **EDITOR)
        self.assertIn(opinion.id, self.system.opinions)  # 记录留档

    def test_pending_opinion_can_be_deleted(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="画面",
            stance="调整构图", content="主体偏小", author="孙画家", role="画家")
        self.system.delete_opinion(opinion.id, **EDITOR)
        self.assertNotIn(opinion.id, self.system.opinions)

    # ----- 冲突会审与重大事实门禁 -----

    def _conflicting_fact_issue(self):
        first = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="依战报", author="李专家", role="党史专家")
        second = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="依回忆录", author="陈专家", role="军史专家")
        self.system.adopt_opinion(first.id, **EDITOR)
        self.system.adopt_opinion(second.id, **EDITOR)
        (issue,) = self.system.issues.values()
        return issue

    def test_conflicting_adopted_opinions_enter_joint_review(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        self.assertEqual(issue.state, "待会审")
        self.assertTrue(issue.is_major_fact)
        self.assertEqual(self.system.pages[self.page.id].status, "联合会审")

    def test_major_fact_blocks_final_draft_until_closed(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        with self.assertRaises(StateError):
            self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.system.close_issue(issue.id, decision="以战报为准: 9月25日", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.assertEqual(self.system.pages[self.page.id].status, "精稿中")

    # ----- 学员异议 -----

    def test_objection_requires_registered_evidence(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        with self.assertRaises(DomainError):
            self.system.submit_objection(
                issue.id, content="日期还有第三种说法", evidence="",
                actor="学员甲", role="学员")
        with self.assertRaises(DomainError):
            self.system.submit_objection(
                issue.id, content="依据未登记", evidence="SRC-999",
                actor="学员甲", role="学员")

    def test_grounded_objection_reopens_closed_issue(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        self.system.close_issue(issue.id, decision="以战报为准", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.system.submit_objection(
            issue.id, content="另一份战报记载不同", evidence=self.source.id,
            actor="学员甲", role="学员")
        self.assertEqual(self.system.issues[issue.id].state, "待会审")
        self.assertEqual(self.system.pages[self.page.id].status, "联合会审")
        with self.assertRaises(StateError):
            self.system.transition_page(self.page.id, "精稿中", **EDITOR)

    # ----- 授权、保密期、出版范围 -----

    def test_expired_license_keeps_page_undeliverable(self):
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), expires=date(2026, 9, 1)),
            base_revision=1, **EDITOR)
        blockers = self.system.page_blockers(self.page.id, scope="国内")
        self.assertTrue(any("过期" in b for b in blockers))
        self.assertFalse(self.system.deliverable(self.page.id, scope="国内"))

    def test_confidentiality_and_scope_checked_per_page_version(self):
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), confidential=date(2026, 12, 31)),
            base_revision=1, **EDITOR)
        blockers = self.system.page_blockers(self.page.id, scope="海外")
        self.assertTrue(any("保密期" in b for b in blockers))
        self.assertTrue(any("出版范围" in b for b in blockers))

    def test_page_with_blockers_cannot_reach_publishable(self):
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), expires=date(2026, 9, 1)),
            base_revision=1, **EDITOR)
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        with self.assertRaises(StateError):
            self.system.transition_page(self.page.id, "可出版", **EDITOR)

    # ----- 依赖陈旧 -----

    def test_stale_dependency_blocks_delivery_until_page_upgraded(self):
        self.system.new_segment_version(
            self.segment.id, text="九月二十五日拂晓, 全营进入伏击阵地。",
            actor="学员甲", base_version=1)
        blockers = self.system.page_blockers(self.page.id)
        self.assertTrue(any("依赖陈旧" in b for b in blockers))
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 2},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲", base_version=1)
        self.assertEqual(self.system.page_blockers(self.page.id), [])

    # ----- 并发与乱序 -----

    def test_concurrent_edit_on_stale_base_is_rejected(self):
        self.system.new_segment_version(
            self.segment.id, text="第二版", actor="学员甲", base_version=1)
        with self.assertRaises(StaleVersionError):
            self.system.new_segment_version(
                self.segment.id, text="第三版", actor="学员乙", base_version=1)

    def test_out_of_order_opinion_is_kept_but_flagged(self):
        self.system.new_segment_version(
            self.segment.id, text="第二版", actor="学员甲", base_version=1)
        opinion = self.system.sign_opinion(
            target_kind="script", target_id=self.segment.id, scope="文字",
            stance="恢复初版措辞", content="针对v1的点评迟到",
            author="赵作家", role="作家", target_version=1)
        self.assertTrue(opinion.outdated)
        self.assertIn(opinion.id, self.system.opinions)

    # ----- 追溯 -----

    def test_panel_trace_recovers_sources_text_version_and_deciders(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="依战报", author="李专家", role="党史专家")
        self.system.adopt_opinion(opinion.id, **EDITOR)
        trace = self.system.panel_trace(self.page.id, self.panel.id)
        self.assertEqual(trace["page_version"], 1)
        self.assertEqual(trace["sources"][0]["citation"], "《八路军战史》第12页")
        self.assertEqual(trace["scripts"][0]["version"], 1)
        self.assertIn("九月二十五日", trace["scripts"][0]["text"])
        self.assertIn("王编辑", trace["deciders"])
        self.assertEqual(trace["opinions"][0]["state"], "采纳")

    # ----- 批量导出 -----

    def _publishable_page(self, title, scopes):
        source = self.system.register_source(
            title=f"{title}史料", citation="出处", license=lic(scopes), **EDITOR)
        page = self.system.create_page(
            title=title,
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={source.id: 1},
            actor="学员甲")
        self.system.transition_page(page.id, "待评审", **EDITOR)
        self.system.transition_page(page.id, "精稿中", **EDITOR)
        self.system.transition_page(page.id, "可出版", **EDITOR)
        return page

    def test_export_contains_only_currently_permitted_content(self):
        domestic = self._publishable_page("仅限国内", ("国内",))
        global_page = self._publishable_page("可海外", ("国内", "海外"))
        draft = self.system.create_page(
            title="未完工", script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1}, actor="学员甲")
        result = self.system.export_batch(scope="海外", **EDITOR)
        exported_ids = {p["page_id"] for p in result["pages"]}
        self.assertEqual(exported_ids, {global_page.id})
        self.assertIn(domestic.id, result["excluded"])
        self.assertNotIn(draft.id, exported_ids)
        self.assertNotIn(draft.id, result["excluded"])  # 非可出版页面不进入候选

    def test_export_requires_editor_role(self):
        with self.assertRaises(PermissionDenied):
            self.system.export_batch(scope="国内", actor="学员甲", role="学员")


if __name__ == "__main__":
    unittest.main()
