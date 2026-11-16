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

## 执行环境更正（用户明确指定）

后续统一使用 Conda `agent_study`，不再调用仓库 `.venv`。Python 绝对路径为 `C:/Users/Anconda/envs/agent_study/python.exe`，版本 3.12.14，Pydantic 2.13.4。上文 `.venv` 结果保留为历史证据；T3 已在指定环境重跑 golden + semantic，95 例通过。

该环境缺少 Black 和 mypy，已按项目 dev 依赖范围补装（Black 26.10.1、mypy 1.20.2）；已有 Ruff 0.16.7。发现 pandas 3.0.6 不满足项目 `>=2.2,<3`，已对齐到 2.3.3，未改依赖断言或项目约束。并行 `conda run` 出现临时文件冲突，后续直接调用上述环境 Python。

新版 Ruff 对 str/Enum 报 UP042，但方案显式采用 `MetricLevel(str, Enum)`；保留其类型及 `str()` 行为，仅在该类上注明定点 noqa。全库检查仍有两个未改动的上游问题：`text2sql/errors.py:21` UP042、`tool/memory.py:199` B010，两个文件最近提交均为 baseline。

## 2026-11-16：T4，递归依赖闭包与确定性渲染

- ✔ `closure.py`：按根和依赖声明顺序做 DFS，根先输出、去重；根深度为 0。环、超深、未知依赖分别抛带依赖路径的 `MetricCycleError`、`MetricDepthError`、`UnknownMetricError`。
- ✔ 即使共享依赖已输出，也检查后续路径的深度，避免先走浅路径导致深路径校验漏过。
- ✔ `render.py`：唯一文本出口，固定 metrics → relationships → rules，空段落不输出；保持 SQL 原文、复合键 AND、JOIN 类型及硬约束。保留指标口径、过滤和时间字段，避免只输出公式丢失解释。
- ✔ 新增逐字渲染快照、环检测、未知引用、深度边界、菱形依赖、多根去重测试；无 I/O 隔离导入测试扩展至 closure/render。
- ✔ `tests/semantic/fixtures/closure_expectations.yaml` 手工标注 10 个迁移指标，实际全部逐项匹配；正确率 10/10，期望/实际闭包总条数 24/24；毛利率闭包包含毛利、收入和销售成本。

指定 Conda Python 下验证：全量 `785 passed, 1 skipped in 24.72s`；`mypy openchatbi/semantic` 为 `Success: no issues found in 6 source files`；新增语义代码和测试 Ruff 通过；Black 全库通过（171 文件）；数据严格校验 0 errors / 0 warnings。全库 Ruff 的两个基线问题如上，未修改上游文件。

边界：本次仅增加闭包/渲染纯函数，没有提前填充 T7 门面装配，也未接入 text2sql。渲染器只展示调用方传入的数据，不推断规则优先级或发明 JOIN 键。下一任务 T5 关系图与 JOIN 计划，随后按任务实现规则和装配。

## 2026-11-16：T5，新增关系图与 JOIN 计划

这是上游没有的新增能力，未重写上游 SQL 生成或选表逻辑。上游复用范围仅为原有的只读外键元数据接口。

done-when：

- ✔ 新增 `join.py`：来源合并、确定性锚表选择、有向图规划、桥接表反补和 `to_sql_clause`。不改变 `SemanticProvider` 签名，门面仍按 T7 装配。
- ✔ 新增 `ForeignKeyJoinSource`、`YamlJoinSource`，显式 load 才读文件/元数据，导入和构造不建立 catalog 客户端。YAML 相对配置文件或仓库根定位，拒绝 BOM、非 UTF-8、结构错误和记录字段拼写错误。
- ✔ FK 与 declared 的同向端点冲突时 declared 为权威；权威定义内部有多个键/类型/基数角色则报带来源的冲突。完全相同定义去重，顺序不依赖来源输入顺序。
- ✔ 复合键完整保留，ON 子句用 AND；手写 SQL 逐字对照通过。上游实际已支持复合 FK 数组（与计划的假设不同），本次未将这个已有读取能力当作新增规划能力。
- ✔ 不可达表进入 `unreachable`，`reason` 说明方向及顺序约束，不猜键、不自动 CROSS JOIN。未知锚表生成空 SQL。
- ✔ INNER 阶段先于 LEFT 阶段，步骤保持引用拓扑。对必须 LEFT→INNER 的路径明确降级为不可达，避免通过乱序或隐式改变 JOIN 类型来满足测试。方向反转仅使用已有反向声明，不自动反转 LEFT。
- ✔ 锚表按未到达请求数、one-to-many 边数、步骤数、名称依次排序；输入边和目标顺序排列组合测试结果逐字一致。

验证（全部使用 `C:/Users/Anconda/envs/agent_study/python.exe`）：

```text
python -m pytest tests/semantic/test_join.py -o addopts= -q
29 passed in 0.59s

python -m pytest tests/semantic -o addopts= -q
112 passed in 5.96s

python -m pytest tests/golden tests/semantic -o addopts= -q
142 passed in 11.21s

python -m pytest -o addopts= -q
818 passed, 1 skipped in 23.09s

python -m mypy openchatbi/semantic
Success: no issues found in 10 source files

python -m ruff check openchatbi/semantic tests/semantic
All checks passed!

python -m black --check .
176 files would be left unchanged.

python scripts/validate_semantic_data.py --strict
0 errors; counts={'metrics': 13, 'rules': 8, 'edges': 6}; warnings=0
```

全库 Ruff 仍为两个已记录的基线问题（UP042 / B010）；新代码无新增问题。为指定环境补装项目已声明的 dev 依赖 types-PyYAML，未新增运行时依赖或修改锁文件。

阶段数字：12 个手工标注的 JOIN 结构场景 12/12 命中，表、键、类型及顺序逐项核对且全部在 SQLite 实际执行；测试数据见 `tests/semantic/fixtures/join_expectations.yaml`。另插入同日异币种汇率仍得到 10 行订单，无订单客户经两层 LEFT JOIN 仍被保留。真实上游 loader 读出 2 条 FK，与 6 条 YAML 声明合并仍为 6 条；另建真实复合外键表验证未截断键数组。禁网/无 key 及新进程导入隔离检查通过。

这一数字衡量新增确定性规划器，不是 LLM 问答端到端命中率；上游原无规划器，不虚构改造前模型准确率。M2 问答链路对比仍需 T8 接入后验证。

遗留边界：当前无自连接别名/同端点多角色选择，不做跨 schema 扫描或方言引号编译。原外键 loader 会把反射异常记录 warning 后返回 []，保留该行为并测试日志可见性；不能把“无边”当成已确认不存在外键。部分规划的 SQL 只包含可达部分，消费方必须先处理 unreachable。未修改任何上游生产文件，未改现有 golden 测试。

## T6：硬规则与显式例外

新增规则匹配、列 require/forbid 和缺表反补。`BusinessRule` 增加可选 `exclude_any` 数据字段，Provider 签名不变；迁移映射显式表达原注释“明确含税时例外”，不在 Python 中写业务词。成本规则按来源指标 notes 显式排除 standard_cost。未处理的 require/forbid 冲突带规则上下文抛错，不静默覆盖。

done-when：20 个收入/成本/毛利问题中最终列集合违规数 0；成本问题反补产品表；含税例外保留 gross_amount 并排除 net_amount。专项测试 23 passed，数据 strict 0 errors / 0 warnings。运行环境仍为 Conda agent_study。

T6 完整验证：全量 841 passed / 1 skipped；mypy 11 源文件通过；Black 全库 178 文件通过。

## T7：配置驱动装配与只读 Provider

新增无副作用的 semantic/config.py，由 config_loader 重导出配置类型；默认关闭。私有配置路径记录真实配置文件位置，语义来源相对它解析；程序化配置则相对仓库根。启用后显式加载 YAML、JSON 或 catalog 来源，按优先级合并同名指标/规则，平局报错；验证别名、依赖、表列引用和关系键，strict=False 的引用问题保存在 layer.warnings。

完整实现 metric/search_metrics/closure/plan_join/rules_for/render；所有读取方法仅操作装配后的快照。无 embedding 时使用别名/展示名/机器键精确词匹配，最长匹配优先，避免“毛利率”被较短的“毛利”抢占。未为 BM25 引入上游 utils 的 LLM/config 导入栈。JSON 原版中文指标需显式 mapping_path 数据映射，不猜英文机器键；优先使用已清洗 YAML。

disabled=False 的常规构造之外，enabled=False 使用空模型快照避免首次 Pydantic 插件构建产生文件读取；没有语义文件、catalog 或客户端 I/O。YAML 来源拒绝记录拼写错误。loader 不 import 源 MVP 项目。

后续接入范围说明：原方案一处写“第4个上游文件停止”，另一处允许6个并明确列出 T7–T9 的接入文件。本轮用户已明确选择完成 T6–T9；按其具体交付范围实施，预计仅 config_loader.py、config.yaml.template、generate_sql.py、sql_graph.py、schema_linking.py 共5个上游文件，仍在C2的6文件上限内。其余新增能力位于 semantic/，不改变 SemanticProvider 的签名。

T7 全量验证 857 passed / 1 skipped；Black 182 文件通过；mypy 14 源文件通过。

## T8：接入 SQL prompt

create_sql_nodes/build_sql_graph 新增可选的 keyword-only semantic 参数；图装配在显式开启时调用 from_config，禁用时走旧参数路径。语义块每次 schema prompt 只追加一次，首次、重试、评分均透传同一问题；无块时完整保留旧派生指标和原列集合。

启用规则时在 schema 注入点再次硬过滤列，并可反补规则表；否则上游原本忽略 selected columns、总是读取全表字段的行为会重新泄露禁用列。没有更改禁用时行为或旧 golden 断言。None/空 provider 逐字比较、启用三段只出现一次及 forbidden Column 不出现均有新增测试。

T8 验证：首次/重试和空 provider 共4例；golden+生成节点专项119 passed；全量861 passed / 1 skipped。原 golden 文件未改动。
