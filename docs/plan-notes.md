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

用户已选择“新增独立的 chatbi_mvp 演示 catalog”，下述 T2 按该授权继续，原物流 catalog 不改动。

## 2026-11-16：T2，数据提取与独立演示 catalog

done-when：

- ✔ JSON 直读指标；AST `literal_eval` 提取规则和关系，不 import 源项目模块。
- ✔ 生成 13 条指标、8 条规则、6 条有向关系；每条带 provenance。复合键拆成结构化键对。
- ✔ `migration.yaml` 显式维护机器键、级别映射和 3 条多步指标标记，Python 中没有业务映射字典，也不按步骤文本前缀嗅探。
- ✔ 未匹配项写入 `_unresolved.yaml`，有效数据的 unresolved 为空；保留 5 条旧版冲突和全部 13 条公式/步骤/时间字段转换证据；4 张干扰表不导入。
- ✔ 新增 `example/semantic/catalog`，5 表、40 字段、30 行 SQLite 演示数据；现有 CatalogStore 能读取全部表和列，公共列索引覆盖全部字段。
- ✔ 严格校验 0 error；完整性测试 47 例通过（超过 20 例要求）；源文件中的顶层异常/动态调用不会被执行。

跨任务说明：T2 按任务顺序先提供离线迁移校验器；领域模型和运行时来源装配属于 T3/T7，后续 loader 仍须执行运行时校验，不能仅相信迁移成功。当前校验不声称具备 AST 级 SQL 语义分析（M5 非目标），而是实际在私有内存 SQLite 上执行 10 条完整 SELECT；3 条多步指标检查显式标记和引用，不伪装为单 SQL 可执行。

验证（仓库 `.venv/Scripts/python.exe`）：

```text
python scripts/validate_semantic_data.py --strict
0 errors; counts={'metrics': 13, 'rules': 8, 'edges': 6}; warnings=0

python -m pytest tests/semantic -o addopts= -q
47 passed in 3.16s

python -m pytest -o addopts= -q
753 passed, 1 skipped in 21.48s

python -m black --check .
162 files would be left unchanged.

python -m ruff check scripts tests/semantic
All checks passed!

python -m ruff check .
1 error: baseline B010, openchatbi/tool/memory.py:199

Live source regeneration: 11 files byte-identical; {'metrics': 13, 'rules': 8, 'edges': 6}
```

额外验证：校验器从仓库外目录运行仍可解析默认路径；禁止 socket 连接且移除 API key 后严格校验通过；演示数据重复初始化后订单仍是 10 行；手算完成订单收入与查询结果一致（66,814,000，订单 8 笔，销量 98）。

遗留业务问题：源“收入”和“含税”规则可同时触发互相冲突的硬约束，已保留并记录，T6 必须显式处理。多步指标的复合时间字段不适合单 `time_field`，当前置空并保留原文，由后续闭包取得各来源字段。SQLite demo 仅保留列、非空、主键、外键和样例值；不等价复制 MySQL 索引/CHECK/默认值（详见数据 README）。

下一步：T3 领域模型、端口和无副作用骨架；随后 T4 闭包与确定性渲染。生产接入尚未开始。

## 2026-11-16：T3，领域模型、端口与空实现

- ✔ 新增 `openchatbi/semantic/{__init__,models,ports,layer}.py`，Provider 的方法签名和返回类型保持方案契约；门面方法明确抛出 `NotImplementedError`，不把骨架当作已可用的执行器。
- ✔ 模型本身可构造和校验；数据对象冻结，bundle 的内部指标字典也阻止原地写入，输入字典修改不影响 bundle；JSON 往返和深拷贝正常。
- ✔ 明确实施方案文字中的 formula/sql_template 二选一，以及非空规则触发词/关系键。为规则和关系各补充 `provenance` 默认字段，保留 T2 已有的来源元数据；消费方接口不变。
- ✔ Pydantic 使用 `defer_build=True`。隔离测试曾发现其默认行为在定义模型类时扫描插件元数据，现推迟到显式构造模型时，未放宽无 I/O 断言。
- ✔ 从仓库外目录启动新进程，禁止应用文件读取及 socket 构造后，包、模型、端口、门面分别 import 成功，未引入 catalog/config/LLM/Chroma。
- ✔ 现有 13 指标、8 规则、6 关系均可转成模型，来源保留。

验证：模型与导入测试 `18 passed`；`mypy openchatbi/semantic` 为 `Success: no issues found in 4 source files`；golden + semantic `95 passed in 9.09s`；全量 `771 passed, 1 skipped in 22.21s`；Black 全库通过（167 文件）；新增文件 Ruff 通过；严格数据校验 0 errors / 0 warnings。

边界：上游生产文件改动 0，未加载配置、文件来源或建立全局 provider。下一任务 T4 仅实现独立的闭包与渲染纯函数；门面的配置装配按 T7 落地。
