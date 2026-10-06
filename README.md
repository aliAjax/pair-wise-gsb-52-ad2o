# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限和计划版本和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/recon_rules.py`：对账链规则——同意有效期计分、回执校验、批次与快照角色。
- `src/recon_repository.py`：同意、回执批次、回执、累计台账、复查快照与审计事件的存储；单条回执的入账、台账、审计、检查点在同一事务提交。
- `src/recon_service.py`：批次提交/续传、同意登记与撤回、依据变化重算、复查快照编排。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 对账链接口

支持计划只记累计分钟，回执明细、同意依据和复查快照各自落库并串成可续作的对账链：

- `POST /api/consents`：登记监护人同意，请求体`{"plan_reference":"...","guardian":"...","scope":"...","valid_from":"YYYY-MM-DD","valid_to":"YYYY-MM-DD"}`（`parent_rep`/`case_manager`）。同一计划的有效同意窗口不允许重叠。
- `GET /api/consents?plan_reference=`：同意列表。
- `POST /api/consents/{id}/withdraw`：撤回同意，请求体`{"effective_date":"YYYY-MM-DD"}`（`parent_rep`）。撤回后依据版本递增，未确认回执失效重算；已入账分钟不冲掉。
- `POST /api/batches`：提交回执批次，请求体`{"batch_no":"...","provider":"...","receipts":[{"external_id":"...","plan_reference":"...","service_date":"YYYY-MM-DD","minutes":60}]}`（`provider_ops`/`case_manager`）。批次已存在时需带`expected_version`续传，且回执内容必须与已登记的一致。
- `GET /api/batches/{batch_no}`：批次状态、检查点与入账结果。
- `POST /api/batches/{batch_no}/resume`：从最后完整检查点续传，请求体`{"expected_version":1}`。两名经办同时推进同一批次时，只有先到版本生效，另一方收到409冲突。
- `POST /api/plans/{plan_reference}/snapshots`：确认复查快照（`administrator`），冻结当时的同意依据版本、累计分钟和回执集合，之后依据变化不影响已确认快照。
- `GET /api/plans/{plan_reference}/reconciliation`：对账视图——累计分钟、待核分钟、合规率、当前同意、快照与事件时间线。

计分与恢复语义：

- 回执按`external_id`只入账一次，重传只推进检查点，不重复累计分钟、不追加审计。
- 服务日期落在有效同意窗口内才计分（posted），否则待核（held）；撤回生效日之前且在原有效期内的服务视为当时有效，之后的一律待核。
- 同意登记或撤回都会触发未确认回执重算：被确认快照覆盖的回执保持冻结，已入账的一律不动。
- 批次内每条回执独立事务提交并更新检查点；写库失败时批次标记`failed`，从检查点续传即可完成剩余回执。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及对账链的回执幂等、同意窗口计分、撤回待核、快照依据冻结、批次并发冲突和检查点恢复。
