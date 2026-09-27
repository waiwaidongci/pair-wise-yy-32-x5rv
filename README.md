# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次（批次可选 `equipment_id` 挂到设备）。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情、状态和健康检查。

## 清场复核

换品种时按设备做清场复核，存储、判定、页面分开：`clearance_store.py`（存储）、`clearance_service.py`（判定）、`static/clearance.html`（页面，路由 `/clearance`）。

流程：开工登记（上一品种、上一批次、取样点与残留限度、复核有效期）→ 清洁后填残留读数（进入待复核）→ 另一名质检员在有效期内复核。残留超限、复核迟到或与登记/填数人为同一人时，记录停在待复核并写明原因；改品种、参数或重新取样会使原记录失效并生成新版本，旧记录仍可查询。设备上有清洁中/待复核记录时不得再次开工；批次挂了设备时，清场通过前不能补录检验或申请放行。

- `POST /api/equipment`、`GET /api/equipment`：设备登记与列表（含当前清场状态）。
- `POST /api/clearances`：开工登记。
- `POST /api/clearances/{id}/readings`：填残留读数；重复提交视为重新取样，原结论失效。
- `POST /api/clearances/{id}/review`：复核（质检员，且不能是登记/填数人）。
- `POST /api/clearances/{id}/amend`：修改品种或参数，原记录失效。
- `GET /api/clearances`、`GET /api/clearances/{id}`：列表（含已失效）与详情（含历史版本）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
