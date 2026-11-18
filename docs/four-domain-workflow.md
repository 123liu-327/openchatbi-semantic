# 四域工作流：启动、接口与验收

本轮在原链路之外增加 `four_domain` 模式。外层只拆分能力级子任务，三个子图分别负责语义、Text2SQL、数据整理与交付；工具不需要各自生成计划。新增代码位于 `openchatbi/workflow/`。

## 最短启动

在仓库根目录 `.env` 填写以下内容，聊天模型需要支持工具调用和结构化输出：

```dotenv
OPENAI_API_KEY=你的聊天模型密钥
CHATBI_MODEL=你的聊天模型名称
OPENAI_BASE_URL=你的聊天模型兼容接口
CHATBI_WORKFLOW_MODE=four_domain
QWEN_EMBEDDING_API_KEY=你的千问embedding密钥
QWEN_EMBEDDING_BASE_URL=你的百炼工作空间和地域对应的OpenAI兼容接口
QWEN_EMBEDDING_MODEL=text-embedding-v4
QWEN_EMBEDDING_DIMENSIONS=1024
```

embedding 密钥不复用聊天密钥。接口地址请从当前百炼工作空间取得，不能把聊天供应商的地址自动用作 embedding 地址。[千问同步向量接口](https://help.aliyun.com/en/model-studio/text-embedding-synchronous-api)

```powershell
conda activate agent_study
Set-Location D:\Agent_study\projiect_learn\openchatbi
# 首次或依赖有变化时安装；有意使用当前 Conda Python。
python -m pip install -e .
# 首次及元数据、模型或向量维度变化后执行；这一步会调用千问。
python -m scripts.build_semantic_index
python run_workbench.py
```

打开 `http://localhost:8501`。工作台仍然是同进程 Streamlit + Python/LangGraph，无需另起 FastAPI。列表和按 ID 查询无需 embedding 或索引；相似检索缺少配置、索引过期或服务不可用时显示错误，不改用其他算法。

索引命令读取根目录 `.env`。若设置 `CONFIG_FILE`，使用该配置；否则使用独立演示配置，首次可按 `.env` 中的 `CHATBI_MODEL` 自动准备。生成器保留已有演示数据库，不重置其中的数据。

当前入口使用 four_domain。修改配置后重启工作台以加载图缓存。已有终端环境变量优先于 .env。

## 运行机制与接口

```text
问题解析/能力计划 → 校验 → 调度就绪任务
                         ├─ semantic 子图
                         ├─ text2sql 子图
                         └─ delivery 子图
                      合并结果 → 有限重规划/最终回答
```

外层使用预编译 `StateGraph`，通过条件边和 `Send` 调度；依赖任务串行，独立任务按批次并行。默认并发 3、最多重规划 2 次、累计能力任务 20、注册函数调用 40，均在 `workflow` 中可配置。该实现是批次调度：同批任务的结果在 super-step 结束后统一可见，不承诺某分支刚完成就立即启动下一批。

每个任务包含 `id/domain/operation/args/depends_on`。参数引用形式为：

```json
{"$ref": {"task_id": "sales_2025", "path": "/dataset_ref"}}
```

`path` 是相对上游返回 `data` 的 JSON pointer；引用必须声明依赖。不接受任意 Python 表达式。计划先检查函数、域、依赖环、参数名和预算，引用解析后再验证具体参数类型。已完成成功任务在重规划时保留定义和结果；失败任务的依赖被阻断，最终回答明确说明部分完成。

工具注册包含名称、域、版本、描述、启用状态、Pydantic 输入输出模型及 handler。统一结果包含状态、类型化数据、告警、错误、耗时和快照 ID。三个域都支持 `operation=auto`，由所属域模型选择本域函数；明确操作直接执行，不额外规划。

| 域 | 已注册操作 |
|---|---|
| semantic | `search_tables/search_fields/search_metrics/search_rules/plan_join/resolve_query` |
| text2sql | `query/check_sql/execute_sql` |
| delivery | `prepare_data/export_csv/analyze`；`export_pdf` 已保留但禁用 |

检索函数支持 `mode=list/get/search`。指标 get 的 `id` 使用机器键，例如 `gross_margin_rate`；字段 get 使用完整限定名。字段评分为 `0.7*(1-distance)+0.3*rule_score`，默认 TopK 10、阈值 0.15；禁止列硬排除，必需列不会被 TopK 或阈值截断。表评分为 `1-distance`，默认 TopK 3、阈值 0.2；指标评分为 `1-distance/2`，默认 TopK 3、阈值 0.3，不混入规则分数。递归补充的依赖指标单独标记，未伪造检索分。

索引只保存元数据，不索引业务行。Chroma 使用三类 cosine collection，显式传入千问向量，避免默认本地模型下载。指纹包含语义快照、索引文档、模型、维度与接口地址；同模型下复用未变文档向量，完整新版本成功后原子切换 manifest。失败构建不发布半成品，也不清空现有数据库。历史 collection 暂保留，可由后续维护功能回收。[Chroma 客户端接口](https://docs.trychroma.com/reference/python)

可用 `workflow.retrieval_metadata_path` 指向附加 JSON 元数据：`{"tables":{"表名":"说明"},"fields":{"表名.字段":"说明"}}`。它参与索引指纹，不回写原 catalog。`workflow.retrieval_path` 默认 `data/semantic_retrieval/chroma`。

独立演示配置默认接入 `example/workflow/retrieval_metadata.json`：从原 MVP 静态提取的 5 表、40 字段业务说明，过滤 4 张干扰表，保留来源。运行时无需 MVP 项目仍在本机。需要从源项目重新提取时运行 `python -m scripts.import_retrieval_metadata --source <MVP目录>`，只更新这份附加元数据；随后重新构建索引。

## 数据、交付和扩展

语义层负责业务理解和表、字段、指标选择。resolve_query 的 field_selection 支持 hybrid 和 llm，默认 hybrid。

- hybrid 使用 MVP 的向量与规则 70/30 混合评分选择字段。
- llm 读取候选表的 catalog 字段，复用原 OpenChatBI 的选表选字段提示词和示例。该方式无需字段 embedding。

两种方式都补充必需字段和指标表达式字段、排除禁用字段并校验合法性。表检索和指标检索沿用各自策略。指标纯向量召回后选择根指标，递归补齐依赖，校验规则并渲染规范口径。目录 list/get 保持独立调用。

所有需要生成 SQL 的任务先依赖 `semantic.resolve_query`，再调用 `text2sql.query`；两步的 `question` 必须相同。后者必须提供当前请求的 `semantic_ref`，通过引用传递：

```json
{"tasks":[
  {"id":"meaning","domain":"semantic","operation":"resolve_query","args":{"question":"按国家统计销售额"},"depends_on":[]},
  {"id":"sales","domain":"text2sql","operation":"query","args":{"question":"按国家统计销售额","semantic_ref":{"$ref":{"task_id":"meaning","path":"/semantic_ref"}},"visualize":false},"depends_on":["meaning"]}
]}
```

SQL 子图按 select_anchor → plan_joins → generate_sql → review_sql → authorize_user → execute_sql → score_sql → confidence_gate 执行，按需生成可视化。字段选择在语义层完成。生成、重试及评分使用语义层交付的结构。恢复生成的 SQL 再次经过安全和权限检查。

执行入口强制 SQLGlot AST 只读检查，核对表字段及关系键。直接 execute_sql 也需要当前请求的 semantic_ref 并通过相同检查。权限来自可信服务上下文或 workflow.authorization，配置 principal 和 table_columns。演示启动器生成本地工作台身份及当前 catalog 表授权。自定义配置需提供授权，缺少身份或授权时拒绝执行。

数据通过请求内的 `dataset_ref` 传递。相同请求、问题、语义引用和可视化设置的并发查询共享执行结果；相同语义准备请求也合并。原分析 Agent 的补充查询入口先调用语义域再调用 SQL，不能退回旧 SQL 选表流程；已经有数据时优先使用数据。语义引用持久化于本次请求目录的 `semantics/`，可随检查点恢复，但不能跨请求复用。当前仍沿用项目配置的 Python 执行后端，SQL 检查不等同于 Python 沙箱。

`prepare_data` 和 `export_csv` 支持按列合并，或显式指定 `compare/value_column/group_by`。compare 的差额为第二份减第一份；缺失分组保留为空值，不自动当作零。CSV 从实际数据集序列化，保留中文，使用 UTF-8 BOM，工作台显示下载按钮。数据集副本在 `report_directory/.datasets/<request_id>/`，CSV 在报告目录；不涉及跨请求数据引用或长期 ETL 调度。

MCP 工具沿用既有连接配置，通过 `workflow.external_tool_domains` 将实际工具名分配给 `semantic/text2sql/delivery`。Pydantic 和 JSON Schema 参数均被校验。未分配工具不暴露给新模式；子图的 auto 入口可选择新工具，外层不接收全部插件参数定义。外部服务仍需部署和配置，注册接口不代表该服务已经实现。

`WorkflowRuntime` 预留可信程序注入的 `input_adapter` 和 SQL `authorizer` 回调；模型参数不能修改这两个接口。图片解析、PDF、WebRAG、RL 增强、登录服务、SSO 和行级权限均未交付。原有跨独立来源的多步指标计算也未在本轮新增通用执行引擎。

图缓存区分模型提供者、模式、元数据快照与索引版本；工作台新模式使用独立 `checkpoints-four-domain.db`。界面显示真实计划、任务、函数结果与错误，并可展开完整 JSON；CSV 下载使用原工作台本地文件入口。

## 验收方法

2026-11-17 在 Conda `agent_study` / Python 3.12.14 的最终工程验收：

| 检查 | 结果 |
|---|---|
| 全量测试 | 994 passed / 2 skipped（2026-11-18 更新）；包含显式关闭的真实千问联调 |
| 原语义数据严格校验 | 13 指标、8 规则、6 关系；0 errors / 0 warnings |
| 原 Text2SQL + SQLite + CSV | 确定性模型跑通，收入 66,814,000；CSV 可通过实际工作台下载函数读取 |
| 原业务数据变更 | `example/semantic/` 无 Git 差异；附加索引说明为新增文件 |
| 格式与类型 | Black 全库 207 文件通过；semantic 的 15 个源文件 mypy 通过 |
| Ruff | 修改及新增文件通过；全库仅保留原有 errors.py 的 UP042、memory.py 的 B010 |
| 依赖锁 | `uv lock --check` 通过；新增 SQLGlot 在 agent_study 中安装验证 |

真实千问和聊天服务未作为本次自动验收调用，不据此宣称在线问答或向量召回效果已验收。界面验证使用实际页面函数的自动化调用，未进行真实模型驱动的浏览器人工验收。

```powershell
python -m pytest tests/workflow -q -o addopts=
python -m pytest tests -q -o addopts=
python scripts/validate_semantic_data.py --strict
```

确定性测试覆盖：13 指标离线冷启动、三域工具选择、真实临时 Chroma 持久化与增量更新、原评分公式、必需字段与禁用字段、并发依赖屏障、计划及参数校验、有限重规划、失败分支、检查点轮次隔离、查询去重、中文 CSV、外挂工具 JSON Schema、分析 Agent 补充查询，以及 SQL 检查不可跳过。

千问批次、维度与独立密钥使用模拟客户端验证；真实模型问答质量与真实 embedding 服务需要配置后联调，不将模拟测试作为在线效果证明。原 canonical 数据、SQL 模板、演示种子和 catalog 文件本轮不改写；历史汇率 INNER/LEFT 差异仍保留，应在单独的口径统一任务中处理。

可显式启用单独的千问连通检查（会发出真实 embedding 请求）：

```powershell
$env:CHATBI_LIVE_EMBEDDING_TEST = "1"
python -m pytest tests/workflow/test_integration.py -k live_qwen_embedding -q -o addopts=
Remove-Item Env:CHATBI_LIVE_EMBEDDING_TEST
```

## 当前执行策略和手动验证

外层判断问题范围，目录问题直接进入语义域。取数问题先完成语义选择，再进入 SQL。复杂问题拆成能力级子任务，通过依赖和结果引用连接。独立查询并行，结果交给分析或 CSV 工具。失败阻断依赖任务，保留成功结果，最多重规划两次。

可在问题中指定“字段选择使用 LLM”，外层提示词将 field_selection=llm 传给 resolve_query。默认值为 hybrid，可展开任务参数确认。

1. 输入“有哪些指标”，检查只执行语义查询。
2. 输入“查询收入，字段选择使用 hybrid”，检查 search_fields 及评分，随后进入 SQL。
3. 输入“查询收入，字段选择使用 LLM”，检查语义域的 field_selection 记录，SQL 图中没有字段选择节点。
4. 输入“比较两个已有年份的收入并导出 CSV”，检查独立查询、依赖汇总及下载内容。

阶段输入输出写入 report_directory/.runs/<request_id>/events.jsonl。运行 python -m scripts.evaluate_workflow_run <运行目录> 检查执行前的安全和权限记录。通过 --gold <JSON文件> 可比较指定阶段的标准输出子集。未提供标准输出时，仅检查执行证据完整性。


## 组合配置与业务隔离

工作台与索引构建命令均优先使用 CONFIG_FILE。未设置时继续准备演示配置。example/deployment/config.yaml 通过 includes 组合 database.yaml、business.yaml、retrieval.yaml。数据库 URI 从 CHATBI_DATABASE_URI 注入，业务知识由 catalog、metrics、rules、joins 文件声明，Chroma 目录可独立指定。完整启动示例见根目录 README。

includes 字典递归合并、列表整体替换，入口覆盖包含文件。组合配置相对路径以声明文件为基准，单文件沿用已有路径约定。SQL URI 使用原值，建议 SQLite 使用绝对路径。环境变量只替换字符串值，缺失时报错。包含文件循环和缺失会被拒绝。数据库连接参与图缓存标识，切换后使用对应运行图。


FreshRetailNet 本地接入已完成。业务包入口为 example/freshretail/config.yaml，数据导入命令为 python -m scripts.import_freshretail。独立 SQLite 保存485万行事实和两张匿名维表，索引目录为 data/semantic_retrieval/freshretail/chroma。4项指标使用归一化销量、缺货小时、平均折扣和范围内门店数口径。小时数组保存为 JSON，原始 Parquet 保留。真实千问索引和检索已联调，聊天模型问答效果需手测。配置步骤及问题示例见根目录 README。
