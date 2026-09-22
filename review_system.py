"""连环画创作审稿领域模型。

设计要点：
- 史料、脚本、人物设定、草图、分镜页均为“实体 + 不可变修订链”，互引一律锁定到具体修订号。
- 意见有独立生命周期，专家只能在本人学科范围内签署；采纳后撤回只作逻辑删（tombstone），不可物理删除。
- 冲突意见与学员异议进入联合会审，会审未裁决、重大事实未关闭时页面不能转入精稿。
- 每个画格可沿分镜页修订反查到史料修订、脚本文字版本、采用决定的签署人/会审决定人。
- 素材授权（有效期、出版范围）与史料/素材保密期随页面版本求值；过期、超范围、未过保密期或依赖陈旧的页面不可交付，批量导出只含当下获准内容。
- 所有状态迁移进入只追加的事件日志；修订创建带期望版本号，配合进程内读写锁处理多人并发改稿。
"""

import json
import os
import threading
from datetime import date, datetime

DISCIPLINES = ("军史", "文学", "美术")
ROLES = ("学员", "军史专家", "作家", "画家", "编辑")
ROLE_DISCIPLINE = {
    "军史专家": "军史",
    "作家": "文学",
    "画家": "美术",
}
STAGES = ("构思中", "待评审", "联合会审", "精稿中", "可出版")
CLOSED_COMMENT_STATUSES = ("addressed", "withdrawn", "dismissed")

ARTIFACT_KINDS = ("record", "script", "character", "sketch", "page")
KIND_LABEL = {
    "record": "史料",
    "script": "脚本",
    "character": "人物设定",
    "sketch": "草图",
    "page": "分镜页",
}


class DomainError(Exception):
    """业务规则冲突，status 供 HTTP 层映射。"""

    def __init__(self, message, status=409, code=None):
        super().__init__(message)
        self.status = status
        self.code = code or "domain_error"


def _today_iso():
    return date.today().isoformat()


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_day(value):
    if not value:
        return None
    return date.fromisoformat(value)


class ReviewSystem:
    """线程安全的审稿系统；data_path 给定时按 JSON 快照持久化（原子写入）。"""

    def __init__(self, data_path=None, clock=None):
        self._lock = threading.RLock()
        self._data_path = data_path
        self._clock = clock or _today_iso
        self.store = self._empty_store()
        if data_path and os.path.exists(data_path):
            with open(data_path, encoding="utf-8") as handle:
                self.store = json.load(handle)

    @staticmethod
    def _empty_store():
        return {
            "counters": {},
            "users": {},
            "projects": {},
            "artifacts": {kind: {} for kind in ARTIFACT_KINDS},
            "materials": {},
            "comments": {},
            "reviews": {},
            "events": [],
        }

    # ------------------------------------------------------------------ 基础

    def _save(self):
        if not self._data_path:
            return
        tmp = f"{self._data_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.store, handle, ensure_ascii=False)
        os.replace(tmp, self._data_path)

    def _event(self, action, actor=None, **details):
        seq = len(self.store["events"]) + 1
        event = {"seq": seq, "at": _now_iso(), "actor": actor, "action": action}
        event.update(details)
        self.store["events"].append(event)

    def _next_id(self, prefix):
        counters = self.store["counters"]
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]}"

    def _user(self, user_id):
        user = self.store["users"].get(user_id)
        if not user:
            raise DomainError(f"用户不存在：{user_id}", status=404, code="user_not_found")
        return user

    def _project(self, project_id):
        project = self.store["projects"].get(project_id)
        if not project:
            raise DomainError(f"项目不存在：{project_id}", status=404, code="project_not_found")
        return project

    def _artifact(self, kind, artifact_id):
        artifact = self.store["artifacts"][kind].get(artifact_id)
        if not artifact:
            raise DomainError(
                f"{KIND_LABEL[kind]}不存在：{artifact_id}", status=404, code="artifact_not_found"
            )
        return artifact

    def _revision(self, revision_id):
        kind, rest = revision_id.split(":", 1)
        artifact_id, version_token = rest.split("#", 1)
        artifact = self._artifact(kind, artifact_id)
        version = int(version_token[1:])
        if not (1 <= version <= len(artifact["revisions"])):
            raise DomainError(f"修订不存在：{revision_id}", status=404, code="revision_not_found")
        return artifact, artifact["revisions"][version - 1]

    @staticmethod
    def _rev_id(kind, artifact_id, version):
        return f"{kind}:{artifact_id}#v{version}"

    @staticmethod
    def _latest(artifact):
        return artifact["revisions"][-1]

    def snapshot(self, project_id):
        with self._lock:
            self._project(project_id)
            return {
                "project": self.store["projects"][project_id],
                "materials": {
                    mid: item
                    for mid, item in self.store["materials"].items()
                    if item["project_id"] == project_id
                },
                "artifacts": {
                    kind: {
                        aid: item
                        for aid, item in bucket.items()
                        if item["project_id"] == project_id
                    }
                    for kind, bucket in self.store["artifacts"].items()
                },
                "comments": {
                    cid: item
                    for cid, item in self.store["comments"].items()
                    if item["project_id"] == project_id
                },
                "reviews": {
                    rid: item
                    for rid, item in self.store["reviews"].items()
                    if item["project_id"] == project_id
                },
            }

    # ------------------------------------------------------------------ 人员

    def create_user(self, name, roles=None, disciplines=None):
        with self._lock:
            roles = list(roles or [])
            bad_roles = [role for role in roles if role not in ROLES]
            if bad_roles:
                raise DomainError(f"未知角色：{bad_roles}", status=400, code="bad_role")
            inferred = [ROLE_DISCIPLINE[role] for role in roles if role in ROLE_DISCIPLINE]
            disc = list(dict.fromkeys([*(disciplines or []), *inferred]))
            bad_disc = [item for item in disc if item not in DISCIPLINES]
            if bad_disc:
                raise DomainError(f"未知学科：{bad_disc}", status=400, code="bad_discipline")
            user_id = self._next_id("u")
            user = {"id": user_id, "name": name, "roles": roles, "disciplines": disc}
            self.store["users"][user_id] = user
            self._event("user_created", name, user_id=user_id)
            self._save()
            return user

    def create_project(self, name, actor=None):
        with self._lock:
            pid = self._next_id("p")
            project = {"id": pid, "name": name, "status": STAGES[0], "created_at": _now_iso()}
            self.store["projects"][pid] = project
            self._event("project_created", actor, project_id=pid, name=name)
            self._save()
            return project

    # ------------------------------------------------------------------ 素材

    def create_material(self, project_id, name, license_scopes, starts_at, expires_at,
                        confidential_until=None, actor=None):
        """登记素材授权。license_scopes 为获准出版范围标签；日期均为 YYYY-MM-DD。"""
        with self._lock:
            self._project(project_id)
            mid = self._next_id("m")
            material = {
                "id": mid,
                "project_id": project_id,
                "name": name,
                "license": {
                    "scopes": list(license_scopes),
                    "starts_at": starts_at,
                    "expires_at": expires_at,
                },
                "confidential_until": confidential_until,
                "created_at": _now_iso(),
            }
            self.store["materials"][mid] = material
            self._event("material_registered", actor, project_id=project_id, material_id=mid)
            self._save()
            return material

    # ------------------------------------------------------------------ 修订

    def create_artifact(self, kind, project_id, payload, user_id, page_no=None, name=None,
                        expected_version=None, addresses=None):
        """创建实体首版（expected_version 为空）或追加修订（expected_version 为当前最新版本号）。"""
        with self._lock:
            if kind not in ARTIFACT_KINDS:
                raise DomainError(f"未知实体类型：{kind}", status=400, code="bad_kind")
            self._project(project_id)
            user = self._user(user_id)
            artifact_id = self._next_id(kind[0])

            def build(version):
                rev_id = self._rev_id(kind, artifact_id, version)
                return {
                    "rev_id": rev_id,
                    "version": version,
                    "created_by": user_id,
                    "created_by_name": user["name"],
                    "created_at": _now_iso(),
                    "payload": payload,
                    "addresses": list(addresses or []),
                }

            revision = build(1)
            artifact = {
                "id": artifact_id,
                "kind": kind,
                "project_id": project_id,
                "name": name,
                "page_no": page_no,
                "stage": STAGES[0] if kind == "page" else None,
                "revisions": [revision],
            }
            self._validate_revision(artifact, revision)
            self.store["artifacts"][kind][artifact_id] = artifact
            self._event(
                "artifact_created", user_id, project_id=project_id, kind=kind,
                artifact_id=artifact_id, rev_id=revision["rev_id"],
            )
            self._save()
            return artifact

    def add_revision(self, kind, artifact_id, payload, user_id, expected_version,
                     addresses=None):
        with self._lock:
            artifact = self._artifact(kind, artifact_id)
            current_version = len(artifact["revisions"])
            if expected_version != current_version:
                raise DomainError(
                    f"修订冲突：{KIND_LABEL[kind]}{artifact_id} 当前为 v{current_version}，"
                    f"提交基于 v{expected_version}",
                    status=409,
                    code="version_conflict",
                )
            user = self._user(user_id)
            revision = {
                "rev_id": self._rev_id(kind, artifact_id, current_version + 1),
                "version": current_version + 1,
                "created_by": user_id,
                "created_by_name": user["name"],
                "created_at": _now_iso(),
                "payload": payload,
                "addresses": list(addresses or []),
            }
            self._validate_revision(artifact, revision)
            artifact["revisions"].append(revision)
            self._event(
                "revision_added", user_id, kind=kind, artifact_id=artifact_id,
                rev_id=revision["rev_id"], expected_version=expected_version,
            )
            self._save()
            return artifact

    def _validate_revision(self, artifact, revision):
        """校验分镜页修订的所有互引都锁定到同一项目内确实存在的修订。"""
        if artifact["kind"] != "page":
            return
        payload = revision["payload"]
        project_id = artifact["project_id"]
        pinned = {}

        def check(rev_id, expected_kind, label):
            try:
                target, target_rev = self._revision(rev_id)
            except DomainError:
                raise DomainError(f"{label}引用不存在：{rev_id}", 400, "bad_reference")
            except (ValueError, KeyError):
                raise DomainError(f"{label}引用格式错误：{rev_id}", 400, "bad_reference")
            if target["kind"] != expected_kind or target["project_id"] != project_id:
                raise DomainError(f"{label}引用越界：{rev_id}", 400, "bad_reference")
            pinned[rev_id] = (target, target_rev)

        script_rev_id = payload.get("script_version")
        if not script_rev_id:
            raise DomainError("分镜页必须锁定一个脚本版本", 400, "missing_reference")
        check(script_rev_id, "script", "脚本")
        for rev_id in payload.get("record_versions", {}).values():
            check(rev_id, "record", "史料")
        for rev_id in payload.get("design_versions", {}).values():
            check(rev_id, "character", "人物设定")
        for rev_id in payload.get("sketch_versions", {}).values():
            check(rev_id, "sketch", "草图")

        script_rev = pinned[script_rev_id][1]
        paragraph_ids = {p["id"] for p in script_rev["payload"].get("paragraphs", [])}
        for panel in payload.get("panels", []):
            if not panel.get("panel_id"):
                raise DomainError("画格缺少 panel_id", 400, "bad_panel")
            if panel.get("paragraph_id") not in paragraph_ids:
                raise DomainError(
                    f"画格{panel['panel_id']}引用的脚本段落在锁定脚本版本中不存在："
                    f"{panel.get('paragraph_id')}",
                    400,
                    "bad_reference",
                )
            for label, key, map_key in (
                ("史料", "record_id", "record_versions"),
                ("人物设定", "character_id", "design_versions"),
                ("草图", "sketch_id", "sketch_versions"),
            ):
                value = panel.get(key)
                if value and value not in payload.get(map_key, {}):
                    raise DomainError(
                        f"画格{panel['panel_id']}引用的{label}{value}未在页面版本中锁定",
                        400,
                        "bad_reference",
                    )

    # ------------------------------------------------------------------ 意见

    def create_comment(self, author_id, discipline, target_revision_id, summary,
                       severity="normal", body=None):
        with self._lock:
            author = self._user(author_id)
            if discipline not in DISCIPLINES:
                raise DomainError(f"未知学科：{discipline}", 400, "bad_discipline")
            # 专家只能在自己专业范围内出具意见。
            if discipline not in author["disciplines"]:
                raise DomainError(
                    f"{author['name']}无权出具{discipline}学科意见", status=403, code="out_of_scope"
                )
            if severity not in ("normal", "major"):
                raise DomainError("severity 仅支持 normal/major", 400, "bad_severity")
            target, target_rev = self._revision(target_revision_id)
            cid = self._next_id("c")
            comment = {
                "id": cid,
                "project_id": target["project_id"],
                "author_id": author_id,
                "author_name": author["name"],
                "discipline": discipline,
                "severity": severity,
                "target_revision_id": target_revision_id,
                "target_kind": target["kind"],
                "summary": summary,
                "body": body,
                "status": "open",
                "signers": [],
                "history": [
                    {"at": _now_iso(), "actor": author_id, "action": "created", "note": None}
                ],
                "ever_adopted": False,
            }
            self.store["comments"][cid] = comment
            self._event(
                "comment_created", author_id, project_id=target["project_id"],
                comment_id=cid, target=target_revision_id, discipline=discipline,
                severity=severity,
            )
            self._save()
            return comment

    def sign_comment(self, comment_id, user_id):
        """专家在本人学科范围内签署意见；签署是意见产生约束力的前提。"""
        with self._lock:
            comment = self.store["comments"].get(comment_id)
            if not comment:
                raise DomainError(f"意见不存在：{comment_id}", 404, "comment_not_found")
            user = self._user(user_id)
            if comment["discipline"] not in user["disciplines"]:
                raise DomainError(
                    f"{user['name']}不能签署{comment['discipline']}学科意见",
                    status=403,
                    code="out_of_scope",
                )
            if comment["status"] not in ("open", "signed", "adopted"):
                raise DomainError(
                    f"意见状态为 {comment['status']}，不能再签署", code="illegal_transition"
                )
            if user_id not in comment["signers"]:
                comment["signers"].append(user_id)
            if comment["status"] == "open":
                comment["status"] = "signed"
            comment["history"].append(
                {"at": _now_iso(), "actor": user_id, "action": "signed", "note": None}
            )
            self._event("comment_signed", user_id, comment_id=comment_id)
            self._save()
            return comment

    def adopt_comment(self, comment_id, user_id):
        with self._lock:
            comment = self.store["comments"].get(comment_id)
            if not comment:
                raise DomainError(f"意见不存在：{comment_id}", 404, "comment_not_found")
            user = self._user(user_id)
            if "编辑" not in user["roles"]:
                raise DomainError("只有编辑可以采纳意见", status=403, code="forbidden")
            if comment["status"] not in ("signed", "adopted"):
                raise DomainError(
                    f"意见须先经{comment['discipline']}专家签署，当前状态 {comment['status']}",
                    code="illegal_transition",
                )
            comment["status"] = "adopted"
            comment["ever_adopted"] = True
            comment["adopted_by"] = user_id
            comment["history"].append(
                {"at": _now_iso(), "actor": user_id, "action": "adopted", "note": None}
            )
            self._event("comment_adopted", user_id, comment_id=comment_id)
            self._save()
            return comment

    def withdraw_comment(self, comment_id, user_id, note=None):
        """采纳（或签署）后撤回：保留记录与决定人，进入 withdrawn，任何人均不能删除。"""
        with self._lock:
            comment = self.store["comments"].get(comment_id)
            if not comment:
                raise DomainError(f"意见不存在：{comment_id}", 404, "comment_not_found")
            user = self._user(user_id)
            if user_id not in comment["signers"]:
                raise DomainError("只有原签署专家可以撤回意见", status=403, code="forbidden")
            if comment["status"] not in ("signed", "adopted"):
                raise DomainError(
                    f"意见状态为 {comment['status']}，不能撤回", code="illegal_transition"
                )
            comment["status"] = "withdrawn"
            comment["withdrawn_by"] = user_id
            comment["history"].append(
                {"at": _now_iso(), "actor": user_id, "action": "withdrawn", "note": note}
            )
            self._event("comment_withdrawn", user_id, comment_id=comment_id, note=note)
            self._save()
            return comment

    def verify_comment(self, comment_id, user_id, note=None):
        """签署专家核验修订已落实，关闭意见（addressed）。"""
        with self._lock:
            comment = self.store["comments"].get(comment_id)
            if not comment:
                raise DomainError(f"意见不存在：{comment_id}", 404, "comment_not_found")
            user = self._user(user_id)
            if user_id not in comment["signers"]:
                raise DomainError("须由原签署专家核验关闭", status=403, code="forbidden")
            if comment["status"] != "adopted":
                raise DomainError(
                    f"意见状态为 {comment['status']}，仅已采纳意见可核验关闭",
                    code="illegal_transition",
                )
            comment["status"] = "addressed"
            comment["verified_by"] = user_id
            comment["history"].append(
                {"at": _now_iso(), "actor": user_id, "action": "addressed", "note": note}
            )
            self._event("comment_verified", user_id, comment_id=comment_id)
            self._save()
            return comment

    def delete_comment(self, comment_id, user_id):
        """编辑仅能删除从未签署/采纳的待处理意见；已采纳后撤回者永久保留。"""
        with self._lock:
            comment = self.store["comments"].get(comment_id)
            if not comment:
                raise DomainError(f"意见不存在：{comment_id}", 404, "comment_not_found")
            user = self._user(user_id)
            if "编辑" not in user["roles"]:
                raise DomainError("只有编辑可以删除意见", status=403, code="forbidden")
            if comment["status"] != "open" or comment["signers"] or comment["ever_adopted"]:
                raise DomainError(
                    "已签署、已采纳或采纳后撤回的意见不得删除，只能随审计记录保留",
                    status=409,
                    code="comment_protected",
                )
            del self.store["comments"][comment_id]
            self._event("comment_deleted", user_id, comment_id=comment_id, snapshot=comment)
            self._save()
            return {"deleted": comment_id}

    # ------------------------------------------------------------------ 会审

    def raise_conflict(self, project_id, comment_ids, reason, user_id):
        with self._lock:
            self._project(project_id)
            user = self._user(user_id)
            if "编辑" not in user["roles"]:
                raise DomainError("冲突会审由编辑发起", status=403, code="forbidden")
            comments = self._load_case_comments(project_id, comment_ids)
            disciplines = {c["discipline"] for c in comments}
            if len(comments) < 2 or len(disciplines) < 2:
                raise DomainError(
                    "联合会审须包含至少两条、且来自两个不同学科的冲突意见",
                    code="bad_review",
                )
            return self._open_review("conflict", project_id, comment_ids, reason, user_id, comments)

    def raise_objection(self, comment_id, user_id, rationale, evidence_revision_ids=None):
        """学员对意见提交有依据的异议，须引用史料修订作为依据，进入联合会审裁决。"""
        with self._lock:
            comment = self.store["comments"].get(comment_id)
            if not comment:
                raise DomainError(f"意见不存在：{comment_id}", 404, "comment_not_found")
            user = self._user(user_id)
            if "学员" not in user["roles"]:
                raise DomainError("只有学员可以提交异议", status=403, code="forbidden")
            evidence = list(evidence_revision_ids or [])
            if not evidence:
                raise DomainError("异议必须附史料依据（evidence_revision_ids）", 400, "no_evidence")
            for rev_id in evidence:
                target, _rev = self._revision(rev_id)
                if target["kind"] != "record" or target["project_id"] != comment["project_id"]:
                    raise DomainError(f"异议依据须为本项目史料修订：{rev_id}", 400, "bad_evidence")
            if comment["status"] in CLOSED_COMMENT_STATUSES:
                raise DomainError(
                    f"意见已关闭（{comment['status']}），不能再提异议", code="illegal_transition"
                )
            return self._open_review(
                "objection", comment["project_id"], [comment_id], rationale, user_id,
                [comment], evidence=evidence,
            )

    def _load_case_comments(self, project_id, comment_ids):
        comments = []
        for cid in comment_ids:
            comment = self.store["comments"].get(cid)
            if not comment or comment["project_id"] != project_id:
                raise DomainError(f"意见不存在于本项目：{cid}", 404, "comment_not_found")
            if comment["status"] in CLOSED_COMMENT_STATUSES:
                raise DomainError(
                    f"意见 {cid} 已关闭（{comment['status']}），不再进入会审",
                    code="illegal_transition",
                )
            comments.append(comment)
        return comments

    def _open_review(self, kind, project_id, comment_ids, reason, user_id, comments,
                     evidence=None):
        rid = self._next_id("r")
        review = {
            "id": rid,
            "project_id": project_id,
            "kind": kind,
            "comment_ids": list(comment_ids),
            "reason": reason,
            "raised_by": user_id,
            "evidence_revision_ids": evidence or [],
            "status": "open",
            "disciplines": sorted({c["discipline"] for c in comments}),
            "ruling": None,
            "created_at": _now_iso(),
        }
        self.store["reviews"][rid] = review
        for comment in comments:
            comment["status"] = "in_review" if comment["status"] in ("open", "signed", "adopted") else comment["status"]
            comment["history"].append(
                {"at": _now_iso(), "actor": user_id, "action": "review_opened", "note": rid}
            )
        self._event(
            "review_opened", user_id, project_id=project_id, review_id=rid, kind=kind,
            comment_ids=list(comment_ids),
        )
        self._save()
        return review

    def rule_review(self, review_id, decided_by, decisions, rationale):
        """联合会审裁决：决定人学科必须覆盖涉案全部学科且不少于两个学科。

        decisions: {comment_id: "uphold" | "revise" | "dismiss"}
        uphold/revise 意见生效为 adopted，dismiss 则推翻为 dismissed。
        """
        with self._lock:
            review = self.store["reviews"].get(review_id)
            if not review:
                raise DomainError(f"会审不存在：{review_id}", 404, "review_not_found")
            if review["status"] != "open":
                raise DomainError("会审已裁决", code="illegal_transition")
            deciders = [self._user(uid) for uid in decided_by]
            panel_disciplines = {d for user in deciders for d in user["disciplines"]}
            missing = set(review["disciplines"]) - panel_disciplines
            if missing:
                raise DomainError(
                    f"会审决定人缺少学科：{sorted(missing)}", status=403, code="panel_incomplete"
                )
            if len(panel_disciplines) < 2:
                raise DomainError(
                    "联合会审须由两个及以上学科专家共同决定", status=403, code="panel_incomplete"
                )
            allowed = {"uphold", "revise", "dismiss"}
            if set(decisions) != set(review["comment_ids"]) or any(
                value not in allowed for value in decisions.values()
            ):
                raise DomainError("须对会审内每条意见给出 uphold/revise/dismiss", 400, "bad_ruling")
            outcome = {
                "at": _now_iso(),
                "decided_by": list(decided_by),
                "decided_by_names": [u["name"] for u in deciders],
                "decisions": dict(decisions),
                "rationale": rationale,
            }
            review["status"] = "ruled"
            review["ruling"] = outcome
            for cid, verdict in decisions.items():
                comment = self.store["comments"][cid]
                if verdict == "dismiss":
                    comment["status"] = "dismissed"
                else:
                    comment["status"] = "adopted"
                    comment["ever_adopted"] = True
                comment["history"].append(
                    {"at": outcome["at"], "actor": ",".join(decided_by),
                     "action": f"review_{verdict}", "note": review_id}
                )
            self._event(
                "review_ruled", ",".join(decided_by), review_id=review_id,
                decisions=dict(decisions),
            )
            self._save()
            return review

    # ------------------------------------------------------------------ 阶段

    def advance_page(self, page_id, user_id, stage):
        with self._lock:
            page = self._artifact("page", page_id)
            current = STAGES.index(page["stage"])
            if stage not in STAGES:
                raise DomainError(f"未知阶段：{stage}", 400, "bad_stage")
            target = STAGES.index(stage)
            # “联合会审”为条件阶段：无开放会审时允许从待评审直接进入精稿中。
            normal_step = target == current + 1
            skip_review = (
                page["stage"] == "待评审" and stage == "精稿中"
            )
            if not (normal_step or skip_review):
                raise DomainError(
                    f"阶段只能向前推进：{page['stage']} → {stage}", code="illegal_transition"
                )
            report = self.deliverability(page_id, as_of=self._clock())
            if stage == "精稿中":
                blocking = [
                    reason for reason in report["blockers"]
                    if reason["code"] in ("major_fact_open", "review_open")
                ]
                if blocking:
                    raise DomainError(
                        "重大事实争议或联合会审尚未关闭，不能转入精稿",
                        code="gate_fact_open",
                    )
            if stage == "可出版":
                if report["blockers"]:
                    raise DomainError(
                        "页面仍存在交付阻断项，不能转为可出版", code="gate_not_deliverable"
                    )
            page["stage"] = stage
            self._event("page_advanced", user_id, page_id=page_id, stage=stage)
            self._save()
            return page

    # ---------------------------------------------------------- 闭包/反查/门禁

    def _page_closure(self, page_revision):
        """返回页面修订锁定的全部修订：脚本、史料、人物设定、草图。"""
        payload = page_revision["payload"]
        closure = {}
        script_rev_id = payload["script_version"]
        closure[script_rev_id] = self._revision(script_rev_id)[1]
        for key in ("record_versions", "design_versions", "sketch_versions"):
            for rev_id in payload.get(key, {}).values():
                closure[rev_id] = self._revision(rev_id)[1]
        return closure

    def _open_reviews_for(self, dependency_artifacts, project_id):
        """dependency_artifacts: 页面依赖到的 (kind, artifact_id) 集合（覆盖整个修订族）。"""
        result = []

        def targets_dependency(comment):
            kind, rest = comment["target_revision_id"].split(":", 1)
            return (kind, rest.split("#", 1)[0]) in dependency_artifacts

        for review in self.store["reviews"].values():
            if review["project_id"] != project_id or review["status"] != "open":
                continue
            if any(targets_dependency(self.store["comments"][cid]) for cid in review["comment_ids"]):
                result.append(review)
        return result

    def deliverability(self, page_id, as_of=None, scope=None):
        """评估分镜页最新修订在指定日期/出版范围下的可交付性。"""
        with self._lock:
            page = self._artifact("page", page_id)
            revision = self._latest(page)
            as_of = as_of or self._clock()
            today = _parse_day(as_of)
            closure_rev_ids = set()
            closure = self._page_closure(revision)
            closure_rev_ids.update(closure)

            blockers, warnings = [], []

            def block(code, message, **extra):
                item = {"code": code, "message": message}
                item.update(extra)
                blockers.append(item)

            # 页面锁定的修订族（含页面自身）：意见/会审按“实体”跟踪，
            # 针对页面旧修订的重大事实未关闭仍阻断，改新版不能绕过。
            dependency_artifacts = {
                tuple(rev_id.split(":", 1)[0:1] + [rev_id.split(":", 1)[1].split("#", 1)[0]])
                for rev_id in closure_rev_ids
            }
            dependency_artifacts.add(("page", page["id"]))

            def on_dependency(comment):
                rev_id = comment["target_revision_id"]
                kind, rest = rev_id.split(":", 1)
                return (kind, rest.split("#", 1)[0]) in dependency_artifacts

            # 1. 依赖陈旧：锁定的任一修订不是该实体最新修订。
            stale_families = set()
            for rev_id in sorted(closure_rev_ids):
                artifact, rev = self._revision(rev_id)
                latest = self._latest(artifact)
                if latest["rev_id"] != rev_id:
                    stale_families.add((artifact["kind"], artifact["id"]))
                    block(
                        "stale_dependency",
                        f"{KIND_LABEL[artifact['kind']]}{artifact.get('name') or artifact['id']}"
                        f"锁定 {rev_id}，最新为 {latest['rev_id']}",
                        pinned=rev_id,
                        latest=latest["rev_id"],
                    )

            # 2. 意见：重大事实未关闭阻断精稿/交付；其余已生效未核验意见在交付时阻断。
            for comment in self.store["comments"].values():
                if comment["project_id"] != page["project_id"]:
                    continue
                if not on_dependency(comment):
                    continue
                pinned_target = comment["target_revision_id"] in closure_rev_ids
                if comment["status"] in CLOSED_COMMENT_STATUSES or comment["status"] == "in_review":
                    continue
                if comment["severity"] == "major" and comment["status"] != "addressed":
                    block(
                        "major_fact_open",
                        f"重大事实意见未关闭：{comment['summary']}（{comment['status']}）",
                        comment_id=comment["id"],
                        discipline=comment["discipline"],
                        target_revision_id=comment["target_revision_id"],
                        pinned=pinned_target,
                    )
                elif comment["status"] in ("signed", "adopted"):
                    block(
                        "comment_unresolved",
                        f"{comment['discipline']}意见尚未核验关闭：{comment['summary']}",
                        comment_id=comment["id"],
                        target_revision_id=comment["target_revision_id"],
                    )
                elif pinned_target:
                    warnings.append(
                        {"code": "comment_pending", "comment_id": comment["id"],
                         "message": f"待签署意见：{comment['summary']}"}
                    )

            # 3. 未关闭的联合会审（含学员异议）。
            for review in self._open_reviews_for(dependency_artifacts, page["project_id"]):
                block(
                    "review_open",
                    f"联合会审 {review['id']}（{review['kind']}）尚未裁决：{review['reason']}",
                    review_id=review["id"],
                )

            # 4. 史料保密期。
            for rev_id, rev in closure.items():
                if not rev_id.startswith("record:"):
                    continue
                confidential_until = rev["payload"].get("confidential_until")
                if confidential_until and today <= _parse_day(confidential_until):
                    block(
                        "confidentiality_active",
                        f"史料 {rev['payload'].get('title')} 保密期至 {confidential_until}",
                        revision=rev_id,
                    )

            # 5. 草图素材授权：有效期与出版范围随页面版本检查。
            material_ids = set()
            for rev_id, rev in closure.items():
                if rev_id.startswith("sketch:"):
                    material_ids.update(rev["payload"].get("material_ids", []))
            for mid in sorted(material_ids):
                material = self.store["materials"].get(mid)
                if not material:
                    block("material_missing", f"素材不存在：{mid}", material_id=mid)
                    continue
                lic = material["license"]
                start, end = _parse_day(lic["starts_at"]), _parse_day(lic["expires_at"])
                if today < start or today > end:
                    block(
                        "license_expired",
                        f"素材 {material['name']} 授权不在有效期内（{lic['starts_at']}~"
                        f"{lic['expires_at']}）",
                        material_id=mid,
                    )
                if scope and scope not in lic["scopes"]:
                    block(
                        "license_scope_denied",
                        f"素材 {material['name']} 未授权出版范围：{scope}",
                        material_id=mid,
                        requested_scope=scope,
                    )
                until = material.get("confidential_until")
                if until and today <= _parse_day(until):
                    block(
                        "confidentiality_active",
                        f"素材 {material['name']} 保密期至 {until}",
                        material_id=mid,
                    )

            return {
                "page_id": page_id,
                "page_no": self._latest(page)["payload"].get("page_no"),
                "stage": page["stage"],
                "revision": revision["rev_id"],
                "as_of": as_of,
                "scope": scope,
                "deliverable": not blockers,
                "blockers": blockers,
                "warnings": warnings,
            }

    def trace_panel(self, page_id, panel_id):
        """反查任一画格：采用的史料、脚本文字版本、素材授权与决定人。"""
        with self._lock:
            page = self._artifact("page", page_id)
            revision = self._latest(page)
            panel = next(
                (item for item in revision["payload"]["panels"] if item["panel_id"] == panel_id),
                None,
            )
            if not panel:
                raise DomainError(f"画格不存在于页面最新版本：{panel_id}", 404, "panel_not_found")
            closure = self._page_closure(revision)
            script_rev = closure[revision["payload"]["script_version"]]
            paragraph = next(
                (p for p in script_rev["payload"]["paragraphs"] if p["id"] == panel["paragraph_id"]),
                None,
            )
            record_rev_id = revision["payload"]["record_versions"].get(panel.get("record_id"))
            character_rev_id = revision["payload"]["design_versions"].get(
                panel.get("character_id")
            )
            sketch_rev_id = revision["payload"]["sketch_versions"].get(panel.get("sketch_id"))

            closure_ids = set(closure)
            family = {
                (rev_id.split(":", 1)[0], rev_id.split(":", 1)[1].split("#", 1)[0])
                for rev_id in closure_ids
            }
            family.add(("page", page["id"]))

            def on_family(rev_id):
                kind, rest = rev_id.split(":", 1)
                return (kind, rest.split("#", 1)[0]) in family

            decisions = []
            for comment in self.store["comments"].values():
                if (
                    comment["project_id"] == page["project_id"]
                    and on_family(comment["target_revision_id"])
                    and comment["status"] != "open"
                ):
                    decisions.append(
                        {
                            "comment_id": comment["id"],
                            "summary": comment["summary"],
                            "discipline": comment["discipline"],
                            "status": comment["status"],
                            "deciders": self._comment_deciders(comment),
                            "target_revision_id": comment["target_revision_id"],
                        }
                    )
            review_decisions = []
            for review in self.store["reviews"].values():
                if review["project_id"] != page["project_id"] or not review["ruling"]:
                    continue
                if any(
                    on_family(self.store["comments"][cid]["target_revision_id"])
                    for cid in review["comment_ids"]
                ):
                    review_decisions.append(
                        {
                            "review_id": review["id"],
                            "kind": review["kind"],
                            "decided_by": review["ruling"]["decided_by_names"],
                            "rationale": review["ruling"]["rationale"],
                            "decisions": review["ruling"]["decisions"],
                        }
                    )
            materials = []
            if sketch_rev_id:
                for mid in closure[sketch_rev_id]["payload"].get("material_ids", []):
                    materials.append(self.store["materials"].get(mid, {"id": mid, "missing": True}))
            return {
                "page_id": page_id,
                "page_revision": revision["rev_id"],
                "panel": panel,
                "script": {
                    "revision": script_rev["rev_id"],
                    "paragraph_id": paragraph["id"] if paragraph else None,
                    "text": paragraph["text"] if paragraph else None,
                    "version": script_rev["version"],
                },
                "historical_record": self._trace_brief(record_rev_id, closure, "title"),
                "character_design": self._trace_brief(character_rev_id, closure, None),
                "sketch": self._trace_brief(sketch_rev_id, closure, None),
                "materials": materials,
                "comment_decisions": decisions,
                "review_decisions": review_decisions,
            }

    @staticmethod
    def _trace_brief(rev_id, closure, title_key):
        if not rev_id:
            return None
        rev = closure[rev_id]
        brief = {"revision": rev_id, "version": rev["version"], "payload": rev["payload"]}
        if title_key:
            brief["title"] = rev["payload"].get(title_key)
        return brief

    def _comment_deciders(self, comment):
        names = []
        for uid in comment["signers"]:
            user = self.store["users"].get(uid)
            if user:
                names.append({"user_id": uid, "name": user["name"], "role": "签署人"})
        if comment.get("verified_by"):
            user = self.store["users"][comment["verified_by"]]
            names.append({"user_id": user["id"], "name": user["name"], "role": "核验关闭人"})
        if comment.get("withdrawn_by"):
            user = self.store["users"][comment["withdrawn_by"]]
            names.append({"user_id": user["id"], "name": user["name"], "role": "撤回人"})
        return names

    # ------------------------------------------------------------------ 导出

    def export(self, project_id, scope, as_of=None):
        """批量导出：只包含当下（as_of）获准且可交付的页面最新版本，其余列入 excluded 并说明原因。"""
        with self._lock:
            self._project(project_id)
            as_of = as_of or self._clock()
            included, excluded = [], []
            pages = (
                (aid, item)
                for aid, item in self.store["artifacts"]["page"].items()
                if item["project_id"] == project_id
            )
            for page_id, page in pages:
                report = self.deliverability(page_id, as_of=as_of, scope=scope)
                if page["stage"] != "可出版":
                    report["blockers"].insert(
                        0,
                        {"code": "stage_not_publishable",
                         "message": f"页面阶段为 {page['stage']}，未达到可出版"},
                    )
                if report["blockers"]:
                    excluded.append(
                        {
                            "page_id": page_id,
                            "page_no": report["page_no"],
                            "stage": page["stage"],
                            "reasons": report["blockers"],
                        }
                    )
                else:
                    revision = self._latest(page)
                    included.append(
                        {
                            "page_id": page_id,
                            "page_no": report["page_no"],
                            "revision": revision["rev_id"],
                            "panels": revision["payload"]["panels"],
                            "trace_refs": {
                                "script": revision["payload"]["script_version"],
                                "records": revision["payload"]["record_versions"],
                                "designs": revision["payload"]["design_versions"],
                                "sketches": revision["payload"]["sketch_versions"],
                            },
                        }
                    )
            return {
                "project_id": project_id,
                "scope": scope,
                "as_of": as_of,
                "included": included,
                "excluded": excluded,
            }
