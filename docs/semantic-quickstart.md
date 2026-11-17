# 简约启动说明

最短网页启动：在项目根目录 `.env` 填写 OPENAI_API_KEY、CHATBI_MODEL 和可选的 OPENAI_BASE_URL（格式见 `.env.example`），激活 Conda agent_study 后运行 `python run_workbench.py`，打开 `http://localhost:8501`。入口自动读取根目录 .env、准备独立演示配置并启动页面；已有终端环境变量优先于 .env。密钥仅保存在本地忽略文件和进程环境中，不写进生成配置。

当前工程启动与回归已通过，汇率表 INNER/LEFT 的源口径差异仍待确认，详见验收说明。以下离线步骤可用于检查现状；在线问答尚未完成最终业务口径验收。

Windows PowerShell，使用已配置的 Conda `agent_study`：

```powershell
conda activate agent_study
Set-Location D:\Agent_study\projiect_learn\openchatbi
python scripts/run_semantic_demo.py --question "毛利率"
```

这会初始化独立的 `data/semantic_demo/` 演示目录，打印指标依赖、JOIN 计划、语义提示块和 SQLite 结果。无需 API key、网络、Docker 或原 MVP 项目。默认不传问题时查询“收入”，预期结果 **66,814,000**。重复启动保留已有演示数据库，不重置其中的数据。

要启动真实模型问答，使用你已有的服务和模型名：

```powershell
$env:PYTHONUTF8 = "1"
$env:OPENAI_API_KEY = "你的密钥"
$env:CHATBI_MODEL = "你的模型名称"
# 自定义兼容接口时，可设置 $env:OPENAI_BASE_URL
python scripts/run_semantic_demo.py --prepare-chat
$env:CONFIG_FILE = (Resolve-Path data/semantic_demo/chat-config.yaml).Path
python run_cli.py "What is the revenue?"
```

交互问答：`python run_cli.py`。网页工作台：`python run_streamlit_ui.py`，打开 `http://localhost:8501`。也可直接执行 `python -m streamlit run sample_ui/streamlit_ui.py --server.port=8501 --server.address=localhost`。不要把启动器 run_streamlit_ui.py 再传给 streamlit run。

Streamlit 页面直接调用同进程中的 Python/LangGraph 后端，无需额外启动 FastAPI。sample_api/async_api.py 是给其他客户端使用的独立 API 示例，不是该工作台的必需依赖。CLI 和工作台均使用同一个 CONFIG_FILE。配置生成器不写入 API key，不覆盖 openchatbi/config.yaml；生成的路径均为绝对路径，embedding 不配置时走上游 BM25 降级。

真实服务是否支持所需的工具调用和结构化输出，仍取决于你配置的模型；本轮没有调用真实模型服务。已验证无密钥离线演示、禁网条件下使用测试模型冷启动真实应用模块，以及固定模型输出的选表→生成→SQLite执行链路。

校验与回归：

```powershell
python scripts/validate_semantic_data.py --strict
python -m pytest tests/golden tests/semantic -o addopts= -q
python -m pytest -o addopts= -q
```

要关闭语义层，将生成配置中的 `semantic.enabled` 改为 `false`。回到原物流示例时，设置 CONFIG_FILE 为原配置文件即可。

本阶段不自动执行跨独立来源的多步指标；离线脚本会标记 `requires_multi_step`，不会伪造结果。含税收入尚需单独的指标定义，当前冲突会明确报错。巡检尚未交付。
