# 语义层迁移最终验收说明

2026-11-17 增量交付：增加可配置的四域编排、独立语义函数、千问/Chroma 检索、SQL AST 执行边界及 CSV 交付。原业务数据和模板保持不变；本轮范围和验证方法见 [四域工作流验收](four-domain-workflow.md)。以下为原迁移阶段的历史验收，不代表已完成在线模型效果验证。

验收日期：2026-11-16。范围：T1–T9（M1–M3），本地 Conda `agent_study` / Python 3.12.14。结论：**工程回归和确定性接入检查通过；最终业务口径验收仍有一项待确认**。真实模型问答效果与 M4 巡检不在本次已验证结果内。

## 最后一项待确认：汇率 JOIN 策略

源 MVP 的 6 条指标 SQL 模板使用 INNER JOIN exchange_rates，而关系声明使用 LEFT JOIN。当前迁移保留原文，尚未替用户改写这个业务策略；已请求用户选择以哪一份为准。不能因为结构校验和标准数据执行通过，就把这一差异当作已解决。

在私有内存库删除 2026-05-15/USD 汇率后，客单价用 INNER 得到 7,761,066.67，用 LEFT 得到 5,820,800；标准完整样本的同值结果不足以验收缺失汇率时的口径。建议按方案的 LEFT 声明统一模板，保留原模板为转换证据；用户确认后需增加缺失汇率回归并更新本结论。此项完成前不宣称规范模板与关系策略完全一致。

## 迁移结果

| 资产 | 最终结果 | 证据 |
|---|---|---|
| 指标 | 13 条，原子/派生/复合层级，依赖使用机器键 | example/semantic/metrics.yaml |
| 规则 | 8 条，列硬排除、必需列、缺表反补及显式含税例外 | rules.yaml、migration.yaml |
| 关系 | 6 条有向关系，含日期+币种复合键 | joins.yaml |
| 独立演示 catalog | 5 表、40 字段、30 行数据 | catalog/ 与 schema.sql |
| 来源 | 有效记录均带 provenance；13 条原公式/步骤/时间字段转换证据保留 | _unresolved.yaml |
| 旧版冲突 | 5 条，明确使用 indicators_full.json，不并行维护旧版 | _unresolved.yaml 的 legacy_conflicts |
| 无法解析项 | 0；4 张干扰表未导入 | unresolved、exclude_tables |
| 知识去重 | Python 不保存行业词汇映射；清洗、中英文别名及例外均在数据文件 | migration.yaml 和静态迁移脚本 |

静态读取 JSON 和 AST literal_eval，不 import 或执行源项目 Python。已有数据可直接启动，不依赖本机仍保留 MVP 源目录；仅重新提取时需要 `--source`。SQLite 演示保留列、非空、主键、外键和样例值，未声称等价迁移 MySQL 的索引、CHECK、默认值和存储引擎设置。

## 验收数字与边界

| 项目 | 结果 | 含义 |
|---|---|---|
| 依赖展开 | 10/10，期望/实际闭包总条数 24/24 | 手工标注，逐项检查顺序与内容，不仅比较数量 |
| JOIN 结构 | 12/12 | 表、键、类型及顺序匹配；全部实际在 SQLite 执行 |
| 单 SQL 指标 | 10/10 可执行 | 使用规范 SQL 模板，不是模型生成准确率 |
| 口径列约束 | 20 条中文问题违规数 0 | 18 条得到可达 schema，2 条独立来源利润问题阻断单查询；不计为已回答 |
| 英文改写 | 4/4 保留约束 | 收入、成本、毛利、毛利率的英文别名与触发规则 |
| 真实节点链路 | 收入 66,814,000 | 固定模型输出，经真实选表、生成、执行节点及 SQLite，非在线模型实测 |
| 默认兼容 | 原 golden 断言不变，None/空 provider 提示词和选表结果相同 | 首次和重试两条路径均覆盖 |
| 启动 | 无 key 离线演示通过；测试模型禁网冷启动通过 | 新进程从仓库外启动真实应用模块 |

来源优先级按数字小者胜，同名同优先级报错；别名冲突、依赖环、未知表列及编码错误带定位。strict=False 的引用问题保存在 warnings 并在图装配时记录，strict=True 直接报错。读取方法操作加载后的快照，不读文件、不调用 LLM/embedding。包及子模块的隔离导入不加载应用 catalog/config/LLM 栈，无全局 provider 单例。

关系规划按声明方向，保持 INNER 先于 LEFT 且 ON 引用已出现的表；不会自动反转 LEFT 或凭空 CROSS JOIN。必须 LEFT 后接 INNER 才能到达的目标会说明不可达。可选误选表可剔除，必需来源不连通时阻止生成单条误导 SQL。多角色外键、自连接别名和需要方言引号的标识符暂不自动处理。

## 验证命令

在项目根、agent_study 环境执行：

```powershell
python scripts/validate_semantic_data.py --strict
python -m pytest tests/golden tests/semantic -o addopts= -q
python -m pytest -o addopts= -q
python -m black --check .
python -m ruff check .
python -m mypy openchatbi/semantic
git diff --stat 8df0d19 HEAD
```

本轮实测：全量 893 passed / 1 skipped；golden + semantic 217 passed；数据结构/引用与模板可执行性校验 0 errors / 0 warnings；mypy 15 源文件通过；Black 全库 187 文件通过。以上校验不覆盖上一节未确认的 JOIN 业务策略。完整输出和提交记录见 docs/plan-notes.md 最后一节。全库 Ruff 的两个既存问题是 text2sql/errors.py:21 的 UP042 和 tool/memory.py:199 的 B010；新增语义层及本次接入文件检查通过，未为消除既有问题扩大生产代码改动。

T1–T9 相对 M0 完成点 c8cd52d，现有上游接入文件共 5 个：config_loader.py、config.yaml.template、text2sql/generate_sql.py、text2sql/sql_graph.py、text2sql/schema_linking.py。从原始 baseline 8df0d19 查看会同时包含此前 M0 清理和惰性导入等变更；两种统计范围不可混用。SemanticProvider 签名保持不变。

## 未完成或未验证的范围

- 按本轮确认，T11–T12 巡检为下一阶段，尚未实现。因此问答/巡检口径一致率、首跑误报率、去重推送与崩溃续跑均未验收，不能写成 100% 或 0 误报。
- 3 条多步指标定义可展开，但未提供自动对齐独立来源时间范围、计算和合并结果的执行器。离线演示明确显示未执行，问答链路会阻断不连通的单 SQL。
- 当前收入规范定义是净额口径。含税规则的触发例外已实现，但含税收入需要单独的指标定义；现阶段检测到指标/规则冲突即报错，不让模型同时接收矛盾定义。
- 真实 LLM 服务、真实业务库及完整 Streamlit 交互未实测。没有编造“改造前 vs 改造后模型准确率”；上游本来没有确定性规划器，12/12 仅代表新增结构规划测试。
- 未做完整 SQL AST 合规校验。硬约束保证进入生成上下文的列集合；不能保证任意模型输出 SQL 都遵守业务口径，生产级 SQL 结果校验属于后续 M5。
- 可选 T10 semantic_probe 工具本轮未注册；离线脚本可查看指标定义和依赖。
- 检索当前按配置的别名/词项匹配，不自动翻译或理解所有自然语言变体；可通过数据文件扩展别名。

启动步骤见 [简约启动说明](semantic-quickstart.md)。
