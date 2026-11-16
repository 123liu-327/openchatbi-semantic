# Semantic Layer 二开执行记录

## 2026-11-16：T1，改造前的行为锁定

- 起点：`c8cd52d`，M0 已存在，工作区干净；工作分支 `codex/semantic-layer`。
- 新增两个方案指定的 golden 测试文件，共 12 例，未改生产代码。
- SQL prompt：首次生成和失败后重试均逐字校验 schema 注入后的 system prompt；覆盖两表顺序、中文、列别名，以及旧 derived_metric 缺失、空值、非空三种情况。时间固定，检索器使用 mock。
- schema linking：锁定合法选择的原样透传、非法表重试后成功、空选择、连续非法表、JSON 解析失败，以及缺少问题时跳过的行为。
- 方案中的“显式 semantic=None / 启用但空 provider”目前没有可调用接口。T1 先锁定现有无 provider 的行为及空 derived_metric；T8/T9 新增参数后必须补测这两个调用分支，不能把当前测试当成已验证启用态。

验证（仓库 `.venv/Scripts/python.exe`）：

```text
python -m pytest tests/golden -o addopts= -q
30 passed in 3.54s

python -m pytest -o addopts= -q
706 passed, 1 skipped in 15.61s

python -m black --check .
159 files would be left unchanged.

python -m ruff check tests/golden/test_golden_semantic_injection.py tests/golden/test_golden_schema_linking.py
All checks passed!

python -m ruff check .
1 error: B010, openchatbi/tool/memory.py:199
```

全库 Ruff 的 B010 来自基线：该文件最近提交为 `8df0d19`，本次未改；按 T1 边界保留并报告。`openchatbi/semantic` 和语义数据尚未创建，语义层 mypy/数据校验不适用于本任务。

## T2 前置核对：目标 catalog 待确定

以 AST 的 `literal_eval` 只读提取本地 `D:/Agent_study/AI.20/chatbi_mvp/join_resolver.py` 的 `TABLE_RELATIONSHIPS`，未 import 或执行源项目。与仓库现有 `example/table_columns.csv` 比较：

- MVP 业务表：`sales_orders`、`dim_customers`、`dim_products`、`exchange_rates`、`finance_expenses`。
- 当前 catalog：`Customers`、`Invoices`、`Order_Items`、`Orders`、`Products`、`Shipment_Items`、`Shipments`。
- 未匹配 5/5，100%，超过方案 §11 的 20% 停止阈值。仓库没有本地 `openchatbi/config.yaml` 可指向其他 catalog。

已请求用户选择目标 catalog。确认前不做业务口径重写或把无法匹配的数据作为有效定义导入。建议另建独立的 MVP 演示 catalog；也可使用用户提供的目标 catalog。
