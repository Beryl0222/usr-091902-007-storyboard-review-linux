"""连环画创作审稿的领域模型与规则。

五条主线:
1. 版本互引: 脚本段落、人物设定、分镜页均按版本演进, 页面版本记录其采用的依赖版本,
   依赖陈旧即不可交付。
2. 专业签署: 党史专家/作家/画家等只签署自己专业范围内的意见。
3. 冲突会审: 结论冲突的已采纳意见进入联合会审, 重大事实未关闭不得转入精稿;
   学员可提交有依据的异议重开议题。
4. 授权检查: 素材授权、保密期、出版范围随页面版本在交付与批量导出时检查。
5. 全程追溯: 任一画格可反查采用的史料、文字版本与决定人。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count


# ---------- 错误 ----------


class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作。"""


class StaleVersionError(DomainError):
    """基于陈旧版本的修改, 需刷新后重试。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


# ---------- 角色与状态词汇 ----------

EXPERT_SCOPES = {
    "党史专家": "史实",
    "军史专家": "史实",
    "作家": "文字",
    "文学编辑": "文字",
    "画家": "画面",
    "美术编辑": "画面",
}
EDITOR_ROLES = {"编辑"}
TRAINEE = "学员"

OPINION_STATES = ("提出", "采纳", "驳回", "撤回")
ISSUE_STATES = ("待会审", "已关闭")
PAGE_STATES = ("构思中", "待评审", "联合会审", "精稿中", "可出版")
PAGE_TRANSITIONS = {
    "构思中": {"待评审"},
    "待评审": {"联合会审", "精稿中"},
    "联合会审": {"精稿中"},
    "精稿中": {"可出版"},
    "可出版": set(),
}


# ---------- 实体 ----------


@dataclass(frozen=True)
class License:
    """素材授权: 出版范围、授权截止日、保密期截止日。"""

    publication_scopes: frozenset
    expires_at: date | None = None
    confidential_until: date | None = None

    def problems(self, scope: str | None, on: date) -> list[str]:
        problems = []
        if self.expires_at is not None and on > self.expires_at:
            problems.append(f"授权已于{self.expires_at.isoformat()}过期")
        if self.confidential_until is not None and on <= self.confidential_until:
            problems.append(f"保密期至{self.confidential_until.isoformat()}届满")
        if scope is not None and scope not in self.publication_scopes:
            problems.append(f"出版范围不含「{scope}」")
        return problems


@dataclass
class HistoricalSource:
    id: str
    title: str
    citation: str
    revision: int
    license: License
    history: list = field(default_factory=list)


@dataclass
class ScriptVersion:
    version: int
    text: str
    author: str
    created_at: datetime


@dataclass
class ScriptSegment:
    id: str
    title: str
    versions: list = field(default_factory=list)

    @property
    def current(self) -> ScriptVersion:
        return self.versions[-1]


@dataclass
class CharacterDesign:
    id: str
    name: str
    versions: list = field(default_factory=list)  # 元素为 dict(version, brief, author, created_at)

    @property
    def current(self) -> dict:
        return self.versions[-1]


@dataclass
class Panel:
    id: str
    index: int
    sketch_ref: str
    page_version: int


@dataclass
class PageVersion:
    version: int
    script_refs: dict  # 脚本段落 id -> 采用的版本号
    design_refs: dict  # 人物设定 id -> 采用的版本号
    source_refs: dict  # 史料 id -> 采用的修订号
    panels: list = field(default_factory=list)
    author: str = ""
    created_at: datetime | None = None


@dataclass
class Page:
    id: str
    title: str
    status: str = "构思中"
    versions: list = field(default_factory=list)
    history: list = field(default_factory=list)

    @property
    def current(self) -> PageVersion:
        return self.versions[-1]


@dataclass
class Opinion:
    id: str
    target_kind: str  # page / script / design / source
    target_id: str
    target_version: int
    scope: str  # 史实 / 文字 / 画面
    stance: str  # 结论标识, 用于冲突检测
    content: str
    author: str
    role: str
    state: str = "提出"
    decided_by: str | None = None
    outdated: bool = False  # 针对的版本已被更新(乱序点评)
    history: list = field(default_factory=list)


@dataclass
class Objection:
    id: str
    author: str
    content: str
    evidence: str  # 依据的史料 id
    created_at: datetime


@dataclass
class Issue:
    id: str
    target_kind: str
    target_id: str
    scope: str
    subject: str
    is_major_fact: bool
    opinion_ids: list = field(default_factory=list)
    state: str = "待会审"
    decision: str | None = None
    decided_by: str | None = None
    objections: list = field(default_factory=list)
    history: list = field(default_factory=list)


# ---------- 审稿系统 ----------


class ReviewSystem:
    """内存中的审稿领域服务; clock 可注入以便测试。"""

    def __init__(self, clock=None):
        self._clock = clock or datetime.now
        self._seq = count(1)
        self.sources: dict[str, HistoricalSource] = {}
        self.segments: dict[str, ScriptSegment] = {}
        self.designs: dict[str, CharacterDesign] = {}
        self.pages: dict[str, Page] = {}
        self.opinions: dict[str, Opinion] = {}
        self.issues: dict[str, Issue] = {}

    # ----- 基础设施 -----

    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._now().date()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq)}"

    @staticmethod
    def _require_editor(role: str):
        if role not in EDITOR_ROLES:
            raise PermissionDenied(f"角色「{role}」无权执行编辑操作")

    def _get(self, table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFoundError(f"{kind}不存在: {key}") from None

    # ----- 史料与授权 -----

    def register_source(self, *, title: str, citation: str, license: License,
                        actor: str, role: str) -> HistoricalSource:
        self._require_editor(role)
        source = HistoricalSource(
            id=self._new_id("SRC"), title=title, citation=citation,
            revision=1, license=license,
        )
        source.history.append(f"{self._now().isoformat()} {actor} 登记史料")
        self.sources[source.id] = source
        return source

    def revise_source(self, source_id: str, *, citation: str, license: License,
                      actor: str, role: str, base_revision: int) -> HistoricalSource:
        self._require_editor(role)
        source = self._get(self.sources, source_id, "史料")
        if base_revision != source.revision:
            raise StaleVersionError(
                f"史料{source_id}当前修订为r{source.revision}, 基于r{base_revision}的修改被拒绝")
        source.citation = citation
        source.license = license
        source.revision += 1
        source.history.append(f"{self._now().isoformat()} {actor} 修订至r{source.revision}")
        return source

    # ----- 脚本段落 -----

    def create_segment(self, *, title: str, text: str, actor: str) -> ScriptSegment:
        segment = ScriptSegment(id=self._new_id("SEG"), title=title)
        segment.versions.append(ScriptVersion(1, text, actor, self._now()))
        self.segments[segment.id] = segment
        return segment

    def new_segment_version(self, segment_id: str, *, text: str, actor: str,
                            base_version: int) -> ScriptVersion:
        segment = self._get(self.segments, segment_id, "脚本段落")
        if base_version != segment.current.version:
            raise StaleVersionError(
                f"脚本段落{segment_id}当前为v{segment.current.version}, "
                f"基于v{base_version}的修改被拒绝")
        version = ScriptVersion(segment.current.version + 1, text, actor, self._now())
        segment.versions.append(version)
        return version

    # ----- 人物设定 -----

    def create_design(self, *, name: str, brief: str, actor: str) -> CharacterDesign:
        design = CharacterDesign(id=self._new_id("DSN"), name=name)
        design.versions.append({
            "version": 1, "brief": brief, "author": actor,
            "created_at": self._now(),
        })
        self.designs[design.id] = design
        return design

    def new_design_version(self, design_id: str, *, brief: str, actor: str,
                           base_version: int) -> dict:
        design = self._get(self.designs, design_id, "人物设定")
        if base_version != design.current["version"]:
            raise StaleVersionError(
                f"人物设定{design_id}当前为v{design.current['version']}, "
                f"基于v{base_version}的修改被拒绝")
        version = {
            "version": design.current["version"] + 1, "brief": brief,
            "author": actor, "created_at": self._now(),
        }
        design.versions.append(version)
        return version

    # ----- 分镜页 -----

    def create_page(self, *, title: str, script_refs: dict, design_refs: dict,
                    source_refs: dict, actor: str) -> Page:
        page = Page(id=self._new_id("PG"), title=title)
        self.pages[page.id] = page
        self._append_page_version(
            page, script_refs=script_refs, design_refs=design_refs,
            source_refs=source_refs, actor=actor)
        return page

    def _validate_refs(self, script_refs, design_refs, source_refs):
        for seg_id, version in script_refs.items():
            segment = self._get(self.segments, seg_id, "脚本段落")
            if not 1 <= version <= segment.current.version:
                raise NotFoundError(f"脚本段落{seg_id}没有版本v{version}")
        for design_id, version in design_refs.items():
            design = self._get(self.designs, design_id, "人物设定")
            if not 1 <= version <= design.current["version"]:
                raise NotFoundError(f"人物设定{design_id}没有版本v{version}")
        for source_id, revision in source_refs.items():
            source = self._get(self.sources, source_id, "史料")
            if not 1 <= revision <= source.revision:
                raise NotFoundError(f"史料{source_id}没有修订r{revision}")

    def _append_page_version(self, page: Page, *, script_refs, design_refs,
                             source_refs, actor) -> PageVersion:
        self._validate_refs(script_refs, design_refs, source_refs)
        version = PageVersion(
            version=len(page.versions) + 1,
            script_refs=dict(script_refs),
            design_refs=dict(design_refs),
            source_refs=dict(source_refs),
            author=actor,
            created_at=self._now(),
        )
        page.versions.append(version)
        page.history.append(
            f"{self._now().isoformat()} {actor} 提交页面v{version.version}")
        return version

    def new_page_version(self, page_id: str, *, script_refs: dict, design_refs: dict,
                         source_refs: dict, actor: str, base_version: int) -> PageVersion:
        page = self._get(self.pages, page_id, "分镜页")
        if base_version != page.current.version:
            raise StaleVersionError(
                f"分镜页{page_id}当前为v{page.current.version}, "
                f"基于v{base_version}的修改被拒绝")
        return self._append_page_version(
            page, script_refs=script_refs, design_refs=design_refs,
            source_refs=source_refs, actor=actor)

    def add_panel(self, page_id: str, *, page_version: int, index: int,
                  sketch_ref: str, actor: str) -> Panel:
        page = self._get(self.pages, page_id, "分镜页")
        version = next((v for v in page.versions if v.version == page_version), None)
        if version is None:
            raise NotFoundError(f"分镜页{page_id}没有版本v{page_version}")
        panel = Panel(
            id=self._new_id("PN"), index=index,
            sketch_ref=sketch_ref, page_version=page_version,
        )
        version.panels.append(panel)
        page.history.append(
            f"{self._now().isoformat()} {actor} 在v{page_version}添加画格{panel.id}")
        return panel

    # ----- 专业签署与意见生命周期 -----

    def _target_version(self, target_kind: str, target_id: str) -> int:
        if target_kind == "page":
            return self._get(self.pages, target_id, "分镜页").current.version
        if target_kind == "script":
            return self._get(self.segments, target_id, "脚本段落").current.version
        if target_kind == "design":
            return self._get(self.designs, target_id, "人物设定").current["version"]
        if target_kind == "source":
            return self._get(self.sources, target_id, "史料").revision
        raise DomainError(f"未知的点评对象类型: {target_kind}")

    def sign_opinion(self, *, target_kind: str, target_id: str, scope: str,
                     stance: str, content: str, author: str, role: str,
                     target_version: int | None = None) -> Opinion:
        if role not in EXPERT_SCOPES:
            raise PermissionDenied(f"角色「{role}」不能签署专业意见")
        if EXPERT_SCOPES[role] != scope:
            raise PermissionDenied(
                f"{role}只能签署「{EXPERT_SCOPES[role]}」范围内的意见, 不能签署「{scope}」")
        current = self._target_version(target_kind, target_id)
        if target_version is None:
            target_version = current
        elif not 1 <= target_version <= current:
            raise NotFoundError(f"{target_kind}{target_id}没有版本v{target_version}")
        opinion = Opinion(
            id=self._new_id("OP"), target_kind=target_kind, target_id=target_id,
            target_version=target_version, scope=scope, stance=stance,
            content=content, author=author, role=role,
            outdated=(target_version != current),
        )
        opinion.history.append(f"{self._now().isoformat()} {author}({role}) 提出")
        self.opinions[opinion.id] = opinion
        return opinion

    def _opinion(self, opinion_id: str) -> Opinion:
        return self._get(self.opinions, opinion_id, "意见")

    def adopt_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        self._require_editor(role)
        opinion = self._opinion(opinion_id)
        if opinion.state != "提出":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能采纳")
        opinion.state = "采纳"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 采纳")
        self._detect_conflict(opinion)
        return opinion

    def reject_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        self._require_editor(role)
        opinion = self._opinion(opinion_id)
        if opinion.state != "提出":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能驳回")
        opinion.state = "驳回"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 驳回")
        return opinion

    def withdraw_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        opinion = self._opinion(opinion_id)
        if role not in EDITOR_ROLES and actor != opinion.author:
            raise PermissionDenied("只有编辑或意见作者本人可以撤回意见")
        if opinion.state != "采纳":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能撤回")
        opinion.state = "撤回"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 撤回(原已采纳, 记录留档)")
        return opinion

    def delete_opinion(self, opinion_id: str, *, actor: str, role: str) -> None:
        opinion = self._opinion(opinion_id)
        if opinion.state in ("采纳", "撤回"):
            raise PermissionDenied(
                f"意见{opinion_id}曾被采纳(当前「{opinion.state}」), 任何人不得删除, 记录留档")
        if role not in EDITOR_ROLES and actor != opinion.author:
            raise PermissionDenied("只有编辑或意见作者本人可以删除未采纳的意见")
        del self.opinions[opinion_id]

    # ----- 冲突会审 -----

    def _detect_conflict(self, opinion: Opinion) -> Issue | None:
        peers = [
            other for other in self.opinions.values()
            if other.id != opinion.id
            and other.state == "采纳"
            and other.target_kind == opinion.target_kind
            and other.target_id == opinion.target_id
            and other.scope == opinion.scope
            and other.stance != opinion.stance
        ]
        if not peers:
            return None
        for issue in self.issues.values():
            if (issue.target_kind, issue.target_id, issue.scope) == (
                    opinion.target_kind, opinion.target_id, opinion.scope) \
                    and issue.state == "待会审":
                issue.opinion_ids.append(opinion.id)
                issue.history.append(
                    f"{self._now().isoformat()} 意见{opinion.id}并入会审")
                return issue
        issue = Issue(
            id=self._new_id("IS"),
            target_kind=opinion.target_kind,
            target_id=opinion.target_id,
            scope=opinion.scope,
            subject=f"{opinion.target_kind}:{opinion.target_id} 的{opinion.scope}结论冲突",
            is_major_fact=(opinion.scope == "史实"),
            opinion_ids=[p.id for p in peers] + [opinion.id],
        )
        issue.history.append(f"{self._now().isoformat()} 冲突成立, 进入联合会审")
        self.issues[issue.id] = issue
        if opinion.target_kind == "page":
            page = self.pages[opinion.target_id]
            if page.status == "待评审":
                page.status = "联合会审"
                page.history.append(
                    f"{self._now().isoformat()} 因议题{issue.id}转入联合会审")
        return issue

    def close_issue(self, issue_id: str, *, decision: str, actor: str,
                    role: str) -> Issue:
        self._require_editor(role)
        issue = self._get(self.issues, issue_id, "议题")
        if issue.state != "待会审":
            raise StateError(f"议题{issue_id}已关闭, 不能重复关闭")
        if not decision:
            raise DomainError("关闭议题必须给出会审结论")
        issue.state = "已关闭"
        issue.decision = decision
        issue.decided_by = actor
        issue.history.append(f"{self._now().isoformat()} {actor} 关闭: {decision}")
        return issue

    def submit_objection(self, issue_id: str, *, content: str, evidence: str,
                         actor: str, role: str) -> Objection:
        if role != TRAINEE:
            raise PermissionDenied(f"角色「{role}」不能提交学员异议")
        issue = self._get(self.issues, issue_id, "议题")
        if not evidence:
            raise DomainError("异议必须附史料依据")
        self._get(self.sources, evidence, "史料")  # 依据必须指向已登记史料
        objection = Objection(
            id=self._new_id("OBJ"), author=actor, content=content,
            evidence=evidence, created_at=self._now(),
        )
        issue.objections.append(objection)
        if issue.state == "已关闭":
            issue.state = "待会审"
            issue.history.append(
                f"{self._now().isoformat()} {actor} 提交有依据异议, 议题重开")
            if issue.is_major_fact and issue.target_kind == "page":
                page = self.pages[issue.target_id]
                if page.status in ("精稿中", "可出版"):
                    page.status = "联合会审"
                    page.history.append(
                        f"{self._now().isoformat()} 重大事实议题{issue.id}重开, 退回联合会审")
        else:
            issue.history.append(
                f"{self._now().isoformat()} {actor} 提交异议{objection.id}")
        return objection

    # ----- 页面状态机与交付门禁 -----

    def _open_issues_for_page(self, page: Page, *, major_only: bool = False,
                              fact_only: bool = False) -> list:
        targets = {("page", page.id)}
        current = page.current
        targets |= {("script", sid) for sid in current.script_refs}
        targets |= {("design", did) for did in current.design_refs}
        targets |= {("source", sid) for sid in current.source_refs}
        result = []
        for issue in self.issues.values():
            if issue.state != "待会审":
                continue
            if (issue.target_kind, issue.target_id) not in targets:
                continue
            if major_only and not issue.is_major_fact:
                continue
            if fact_only and issue.scope != "史实":
                continue
            result.append(issue)
        return result

    def _stale_dependencies(self, page: Page) -> list[str]:
        current = page.current
        problems = []
        for seg_id, version in current.script_refs.items():
            latest = self.segments[seg_id].current.version
            if version != latest:
                problems.append(f"脚本段落{seg_id}依赖陈旧: 采用v{version}, 当前v{latest}")
        for design_id, version in current.design_refs.items():
            latest = self.designs[design_id].current["version"]
            if version != latest:
                problems.append(f"人物设定{design_id}依赖陈旧: 采用v{version}, 当前v{latest}")
        for source_id, revision in current.source_refs.items():
            latest = self.sources[source_id].revision
            if revision != latest:
                problems.append(f"史料{source_id}依赖陈旧: 采用r{revision}, 当前r{latest}")
        return problems

    def page_blockers(self, page_id: str, *, scope: str | None = None,
                      on: date | None = None) -> list[str]:
        """返回页面当前不可交付的原因; 空列表表示可交付。"""
        page = self._get(self.pages, page_id, "分镜页")
        on = on or self._today()
        problems = [
            f"事实争议未关闭: {issue.id}({issue.subject})"
            for issue in self._open_issues_for_page(page, fact_only=True)
        ]
        problems.extend(self._stale_dependencies(page))
        for source_id in page.current.source_refs:
            source = self.sources[source_id]
            problems.extend(
                f"史料{source_id}: {problem}"
                for problem in source.license.problems(scope, on)
            )
        return problems

    def deliverable(self, page_id: str, *, scope: str | None = None,
                    on: date | None = None) -> bool:
        return not self.page_blockers(page_id, scope=scope, on=on)

    def transition_page(self, page_id: str, target: str, *, actor: str,
                        role: str) -> Page:
        self._require_editor(role)
        page = self._get(self.pages, page_id, "分镜页")
        if target not in PAGE_TRANSITIONS.get(page.status, set()):
            raise StateError(f"页面{page_id}不能从「{page.status}」转入「{target}」")
        if target == "精稿中":
            majors = self._open_issues_for_page(page, major_only=True)
            if majors:
                raise StateError(
                    "重大事实未关闭, 不能转入精稿: "
                    + ", ".join(issue.id for issue in majors))
        if target == "可出版":
            blockers = self.page_blockers(page_id)
            if blockers:
                raise StateError("页面不可交付, 不能转入可出版: " + "; ".join(blockers))
        page.status = target
        page.history.append(f"{self._now().isoformat()} {actor} 转入「{target}」")
        return page

    # ----- 追溯与导出 -----

    def panel_trace(self, page_id: str, panel_id: str) -> dict:
        page = self._get(self.pages, page_id, "分镜页")
        version = next(
            (v for v in page.versions if any(p.id == panel_id for p in v.panels)), None)
        if version is None:
            raise NotFoundError(f"画格{panel_id}不属于分镜页{page_id}")
        panel = next(p for p in version.panels if p.id == panel_id)
        scripts = []
        for seg_id, seg_version in version.script_refs.items():
            segment = self.segments[seg_id]
            text = segment.versions[seg_version - 1].text
            scripts.append({
                "segment_id": seg_id, "version": seg_version, "text": text,
                "latest_version": segment.current.version,
            })
        sources = []
        for source_id, revision in version.source_refs.items():
            source = self.sources[source_id]
            sources.append({
                "source_id": source_id, "title": source.title,
                "citation": source.citation, "revision": revision,
                "latest_revision": source.revision,
            })
        opinions = [
            {
                "opinion_id": o.id, "scope": o.scope, "stance": o.stance,
                "state": o.state, "author": o.author, "role": o.role,
                "decided_by": o.decided_by, "outdated": o.outdated,
            }
            for o in self.opinions.values()
            if o.target_kind == "page" and o.target_id == page_id
        ]
        issues = [
            {
                "issue_id": i.id, "subject": i.subject, "scope": i.scope,
                "state": i.state, "is_major_fact": i.is_major_fact,
                "decision": i.decision, "decided_by": i.decided_by,
            }
            for i in self.issues.values()
            if i.target_kind == "page" and i.target_id == page_id
        ]
        return {
            "page_id": page_id,
            "page_version": version.version,
            "panel": {"id": panel.id, "index": panel.index,
                      "sketch_ref": panel.sketch_ref},
            "scripts": scripts,
            "sources": sources,
            "opinions": opinions,
            "issues": issues,
            "deciders": sorted({
                x for x in [o.decided_by for o in self.opinions.values()
                            if o.target_kind == "page" and o.target_id == page_id]
                + [i.decided_by for i in self.issues.values()
                   if i.target_kind == "page" and i.target_id == page_id]
                if x
            }),
        }

    def export_batch(self, *, scope: str, page_ids: list | None = None,
                     actor: str, role: str, on: date | None = None) -> dict:
        """批量导出: 只包含当下获准的内容, 被排除的页面附原因。"""
        self._require_editor(role)
        on = on or self._today()
        if page_ids is None:
            candidates = [p for p in self.pages.values() if p.status == "可出版"]
        else:
            candidates = [self._get(self.pages, pid, "分镜页") for pid in page_ids]
        exported, excluded = [], {}
        for page in candidates:
            problems = []
            if page.status != "可出版":
                problems.append(f"页面状态为「{page.status}」, 未达到可出版")
            problems.extend(self.page_blockers(page.id, scope=scope, on=on))
            if problems:
                excluded[page.id] = problems
                continue
            version = page.current
            exported.append({
                "page_id": page.id,
                "title": page.title,
                "page_version": version.version,
                "panels": [
                    {"id": p.id, "index": p.index, "sketch_ref": p.sketch_ref}
                    for p in version.panels
                ],
                "sources": [
                    {
                        "source_id": sid,
                        "citation": self.sources[sid].citation,
                        "revision": rev,
                    }
                    for sid, rev in version.source_refs.items()
                ],
                "scripts": [
                    {"segment_id": sid, "version": v}
                    for sid, v in version.script_refs.items()
                ],
            })
        return {
            "scope": scope,
            "exported_at": self._now().isoformat(),
            "exported_by": actor,
            "pages": exported,
            "excluded": excluded,
        }
