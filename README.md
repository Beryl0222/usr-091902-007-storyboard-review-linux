# 连环画创作审稿

维护史料、脚本、分镜和多学科审稿意见之间的版本依赖。

## 运行

- `python3 service.py --check` 检查基础配置。
- `python3 service.py --port 8000` 启动服务;`GET /health` 为健康检查。
- `npm test` 运行全部测试(服务契约、领域规则、HTTP 接口)。

## 领域规则(domain.py)

- **版本互引**:脚本段落、人物设定、分镜页均按版本演进;页面版本记录其采用的
  脚本版本、设定版本与史料修订号,依赖陈旧即不可交付。
- **专业签署**:党史/军史专家(史实)、作家/文学编辑(文字)、画家/美术编辑(画面)
  只签署自己专业范围内的意见;编辑负责采纳、驳回与状态流转。
- **意见留档**:意见状态为 提出 → 采纳/驳回,采纳后可撤回;曾被采纳的意见
  (含已撤回)任何人不得删除。
- **冲突会审**:同一对象同一专业范围内结论冲突的已采纳意见自动生成会审议题,
  页面转入联合会审;重大事实议题未关闭不得转入精稿。学员可提交附史料依据的
  异议,已关闭的议题会被重开,已进入精稿/可出版的页面退回联合会审。
- **授权检查**:素材授权期、保密期、出版范围随页面版本在交付与导出时检查;
  过期授权、事实争议或依赖陈旧的页面明确保持不可交付。
- **批量导出**:`export_batch` 只包含当下获准的内容,被排除的页面附原因。
- **全程追溯**:`panel_trace` 从任一画格反查采用的史料、文字版本、
  采纳/撤回的意见与决定人。
- **并发与乱序**:所有改稿操作携带 `base_version`/`base_revision` 做乐观并发
  校验(`StaleVersionError`);针对旧版本的迟到点评保留并标记 `outdated`。

## HTTP 接口

`POST /api/<action>`,JSON 请求与响应。action 与领域方法一一对应:
`register_source`、`revise_source`、`create_segment`、`new_segment_version`、
`create_design`、`new_design_version`、`create_page`、`new_page_version`、
`add_panel`、`sign_opinion`、`adopt_opinion`、`reject_opinion`、
`withdraw_opinion`、`delete_opinion`、`close_issue`、`submit_objection`、
`transition_page`、`page_blockers`、`panel_trace`、`export_batch`。

错误映射:404 对象不存在,403 越权,409 版本冲突,400 其他领域规则或请求格式错误。

`fixtures/domain.json` 保存领域名词和状态样例,便于接口联调时保持一致语义。
