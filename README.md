# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意窗口、服务回执分类、依据变化重算、复查快照依据和冲突检查。
- `src/repository.py`：SQLite建表、回执/批次/快照单事务访问和检查点。
- `src/service.py`：用例编排、权限检查、乐观并发、批次续作和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和对账链测试。

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
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作包括 `consent`（同意，可带 `effective_from`）、`withdraw_consent`（撤回，可带 `withdrawn_on`）、`activate`、`log_service`、`review`、`confirm_snapshot`、`amend`、`close`。
- `POST /api/records/{id}/receipt-batches`：批量回传回执，`{"expected_version":N,"batch_ref":"...","receipts":[{"external_id","service_date","minutes"}]}`。
- `POST /api/receipt-batches/{batch_ref}/resume`：从最后完整检查点续作中断批次。
- `GET /api/records/{id}/receipts`：回执列表（`posted`/`pending`/`invalid`）。
- `GET /api/records/{id}/receipt-batches`：批次列表与检查点。
- `GET /api/records/{id}/snapshots`：复查快照列表及冻结依据。

## 对账链语义

- **回执幂等**：`external_id` 全局唯一，重传只推进批次检查点，不重复累计分钟、不追加审计。
- **同意有效期**：同意按半开区间 `[effective_from, withdrawn_on)` 生效；服务日期落在任一窗口内为 `posted` 计分，窗口外为 `pending` 待核不计分。撤回后待核/失效回执重算，原已入账服务不冲掉。
- **依据变化重算**：撤回或重新同意时，未被已确认快照冻结的 `pending`/`invalid` 回执按新依据重新判定（覆盖→`posted`，否则→`invalid`），计划分钟按全部回执从头汇总。
- **复查快照**：`review` 生成未确认快照并封存当时分钟、同意窗口与入账回执指纹；依据变化时未确认快照变为 `stale`；`confirm_snapshot` 确认后依据冻结，其入账回执不参与后续失效重算。
- **批次并发**：批次开账在写锁内校验计划版本，两名经办同批并发只让先到版本推进，另一方收到 409。
- **检查点续作**：每条回执的插入、分钟重算、版本推进、审计与检查点同事务提交；写库失败整体回滚到上一检查点，续作只处理未完成回执，分钟与审计恰好一次。中断时返回 `batch_interrupted`（HTTP 500）并附带 `batch_ref`、`checkpoint` 和续作地址。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
