# 独立 MVP 演示 catalog 与语义数据

本目录与原 `example/` 物流 catalog 分开，不修改默认配置。当前交付为 T2 的离线迁移数据，尚未接入问答链路。

- `metrics.yaml`：13 条指标，别名包含原展示名，依赖使用机器键；优先保留源项目完整 SELECT，避免把带 WHERE/GROUP BY 的原公式当作可内联表达式。
- `rules.yaml`：8 条原规则，字段名保留表限定，强制包含字段的所属表转为 `require_tables`，便于 T6 反补。
- `joins.yaml`：6 条有向声明关系，复合键拆成键对，反向 LEFT JOIN 独立保留；基数按建表脚本的主键判断。
- `migration.yaml`：人工可审阅的机器键、级别映射、多步标记和业务表白名单。业务内容不写在 Python 中。
- `_unresolved.yaml`：未解析项、5 条旧版指标冲突、13 条结构转换证据和待复核说明。正常迁移的 `unresolved` 应为空。旧版 `indicators.json` 不进入有效指标文件。
- `catalog/`：上游 `FileSystemCatalogStore` 可读取的 5 表、40 字段元数据及 `schema.sql`；SQLite 样例共 30 行，不需要源 MySQL。

使用 Conda 的 `agent_study` 环境，从仓库根运行：

```powershell
conda run -n agent_study python scripts/import_chatbi_mvp_kb.py --source D:/Agent_study/AI.20/chatbi_mvp
conda run -n agent_study python scripts/validate_semantic_data.py --strict
conda run -n agent_study python -m pytest tests/semantic/test_data_integrity.py -o addopts= -q
```

脚本内的相对路径均按脚本所在仓库根解析；从其他目录运行时，使用 Python 和脚本的绝对路径即可。当前 Conda Python 为 `C:/Users/Anconda/envs/agent_study/python.exe`，可直接调用，避免并行 `conda run` 的临时文件冲突。可用 `--out`、`--mapping` 指定其他迁移目录和映射文件；校验器支持 `--data`、`--catalog`。不需要 API key、网络或 Docker。

catalog 通过静态解析源 `database/init.sql` 的 CREATE TABLE 和 INSERT VALUES 子集生成。SQLite 演示保留列类型、非空、主键、外键及样例值；MySQL 的索引、CHECK、默认值、数据库设置和存储引擎不迁移。遇到不支持的列定义会报带源文件定位的错误。源 `TABLE_METADATA`、`FIELD_METADATA` 的大段描述没有迁入，演示描述使用表名和列名，后续可人工补充。

`schema.sql` 可用于初始化本地 SQLite，也可像校验器一样加载到 `:memory:`。校验会实际执行 10 条单查询指标；3 条多步指标只检查显式标记和依赖引用，不声称它们已具备执行器。测试核对完成订单数 8、销量 98、收入 66,814,000。

迁移全程不 import 或执行源 Python 模块，仅用 JSON 直读和 AST `literal_eval` 读取常量。多步指标通过 `migration.yaml` 显式标记，源自然语言步骤保存在报告中，不按字符串前缀推断。

已知业务冲突：源“收入”和“含税”规则可同时触发，导致相同列一边必须使用、一边禁止使用。迁移保持原规则并记录，不把结构校验通过当成冲突已解决；T6 必须显式处理规则冲突。多步指标有两个时间字段，当前契约只提供一个 `time_field`，因此置空并在报告中保留原文；T4/T7 应通过依赖拿到各来源时间字段。

来源均使用 `chatbi_mvp:<file>#<key>` 标注；本目录仅迁移本地源项目的演示知识和种子数据，不包含其运行时代码。

T5 新增的关系规划能力可独立使用，配置装配仍留待 T7：

```python
from openchatbi.semantic.join import JoinGraph, merge_edges, to_sql_clause
from openchatbi.semantic.sources.catalog_fk import ForeignKeyJoinSource
from openchatbi.semantic.sources.yaml_file import YamlJoinSource

declared = YamlJoinSource("example/semantic/joins.yaml").load_edges()
# 已有 catalog 时可显式读取外键：
# reflected = ForeignKeyJoinSource(catalog).load_edges()
# edges = merge_edges(reflected, declared)
graph = JoinGraph(declared, tables=("finance_expenses",))
plan = graph.plan_join("", ["sales_orders", "dim_products", "exchange_rates"])
if not plan.unreachable:
    print(to_sql_clause(plan))
```

空锚表表示自动选择，按可达请求表数量、one-to-many 边数、步骤数、表名字典序比较。图严格有向，不自动反转 LEFT JOIN；相同有向端点的 declared 定义覆盖 FK 提示，权威来源自身有冲突则报错，包含来源定位。相同端点的多个外键角色以及自连接需要明确的角色/别名定义，当前契约不足以表达时直接报告，不任意挑一条。

线性计划先遍历 INNER 边，再扩展 LEFT 边，只保留目标所需的桥接表。需要 LEFT 后接 INNER 的路径会列入 `unreachable`，因为当前计划无法同时保持可空行和 JOIN 顺序。`to_sql_clause` 输出可达部分，调用方执行前必须检查 `unreachable`；未知锚表不产生 SQL。当前仅支持无须方言引号的简单标识符和限定表名。

外键来源在 `load_edges()` 才复用原 `DataCatalogLoader.get_foreign_keys()`。实测原接口已支持复合键数组，适配器完整保留；规划器本身是新增能力。catalog 的表名前缀视为该引擎默认数据库的展示限定名，不跨 schema 扫描；FK 返回的 referred_schema 会保留。原 loader 的反射错误会记录 warning 并返回空列表，这一原行为未改动；适配器自己的读取/结构校验异常会带表和记录定位抛出。
