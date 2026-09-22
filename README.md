# 连环画创作审稿

维护史料、脚本、人物设定、草图、分镜页之间的**版本化精确互引**，以及军史/文学/美术分科意见、联合会审、授权保密门禁与可交付导出。

## 核心规则

- **不可变修订链**：五类实体（史料/脚本/人物设定/草图/分镜页）每次改稿生成新修订，互引一律锁定到具体修订号（如 `page:pg1#v2`、`script:s1#v3`）。分镜页的每个画格锁定脚本段落、史料、人物设定与草图版本；引用不存在或跨项目直接拒绝。
- **并发改稿**：追加修订须携带 `expected_version`，多人同时基于旧版提交时只有一人成功，其余收到 `version_conflict`（409），杜绝拿旧脚本继续画或静默覆盖。
- **分科签署**：军史专家/作家/画家只能在本人学科内出具、签署意见；编辑负责采纳，签署人负责核验关闭。
- **联合会审**：跨学科冲突意见由编辑发起，学员可提交**附史料依据**的异议；裁决人学科必须覆盖涉案全部学科（不少于两个）。会审未裁决、重大事实未 `addressed` 时页面不能转入精稿（`gate_fact_open`）。
- **意见删除保护**：已签署/采纳的意见撤回后为 `withdrawn` 墓碑，编辑不能删除，完整历史与决定人保留在事件日志中；只有从未签署的待处理意见可删。
- **授权与保密**：素材授权有有效期与出版范围标签，史料/素材可有保密期；随页面版本、按查询日期实时求值。过期授权、超出版范围、保密期内、锁定修订已陈旧（`stale_dependency`）均明确不可交付。
- **画格反查**：`trace` 返回任一画格采用的史料修订、脚本文字版本、素材授权及全部意见/会审的决定人。
- **批量导出**：只包含查询时点处于「可出版」且全部阻断项为空的页面最新版本，其余页面连同阻断原因列入 `excluded`。

## 运行与测试

```bash
python3 service.py --check            # 基础配置检查
python3 service.py --port 8000        # 启动 HTTP 服务（默认内存存储）
python3 service.py --data state.json  # 持久化到 JSON 快照（原子写入）
npm test                              # 契约 + 领域规则 + HTTP 端到端，共 17 项
```

## 接口概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/users` `/projects` `/materials` | 人员（角色/学科）、项目、素材授权 |
| POST | `/artifacts` | 创建实体首版（`kind` 为 record/script/character/sketch/page） |
| POST | `/artifacts/{kind}/{id}/revisions` | 追加修订（须带 `expected_version`） |
| POST | `/comments` | 专家在本人学科出具意见（`severity` 支持 major） |
| POST | `/comments/{id}` | `action`：sign / adopt / withdraw / verify |
| DELETE | `/comments/{id}` | 编辑删除未签署意见；采纳后撤回者 409 保留 |
| POST | `/reviews/conflicts` | 编辑发起跨学科联合会审 |
| POST | `/reviews/objections` | 学员附史料修订依据提异议 |
| POST | `/reviews/{id}/ruling` | 多学科会签裁决（uphold/revise/dismiss） |
| POST | `/pages/{id}/advance` | 页面阶段推进（重大事实/会审/交付门禁） |
| GET | `/pages/{id}/deliverability?as_of=&scope=` | 可交付性与阻断原因 |
| GET | `/pages/{id}/panels/{panel}/trace` | 画格反查：史料、文字版本、素材、决定人 |
| GET | `/projects/{id}/export?scope=&as_of=` | 批量导出仅含当下获准可出版页面 |
| GET | `/projects/{id}/snapshot` | 项目全部记录（联调/审计） |

`fixtures/domain.json` 保存统一的领域名词、意见状态与交付阻断码语义。
