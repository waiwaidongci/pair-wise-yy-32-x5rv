# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。换品种时按设备做清场复核：清场通过前绑定设备不得补录检验或申请放行。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次（批次可用 `equipment_id` 绑定设备）。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情（含设备清场状态）、状态和健康检查。

## 清场复核

换品种时按设备登记清场：开工登记上一品种、批次、取样点（含限值）和复核有效期；清洁后填写各取样点残留读数；另一名质检员（`inspector`）在有效期内复核。残留超限、复核迟到或与登记/填报人同人，都会停在“待复核”并写明原因；设备被占用（清洁中或待复核）时不得开工，清场通过前绑定该设备的批次不能补录检验或申请放行。改品种、参数或重新取样会让原结论失效并生成新版本，旧记录仍可查询。存储、判定和页面分别在 `clearance_store.py`、`clearance_service.py` 和 `static/clearance.html`。

- `POST /api/equipment`：登记设备（`qa`）。
- `POST /api/clearances`：开工登记（`operator`/`inspector`）。
- `POST /api/clearances/{id}/readings`：清洁后填残留读数。
- `POST /api/clearances/{id}/review`：复核（`inspector`，须与登记、填报不同人）。
- `POST /api/clearances/{id}/revise`、`POST /api/clearances/{id}/resample`：改品种/参数、重新取样，旧版本失效。
- `GET /api/clearances/{id}`、`GET /api/equipment/{id}/clearances`、`GET /api/factories/{id}/clearances`：单条记录、设备全部版本和工厂看板。
- 页面：`/clearance`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
