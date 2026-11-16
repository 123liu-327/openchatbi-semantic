# 参与开发

本仓库是对 [OpenChatBI](https://github.com/zhongyu09/openchatbi)（MIT）的二次开发，`NOTICE` 写明了上游与本仓库各自的著作权范围。

## 约定

1. **上游代码尽量不改**。新能力放进新目录（`openchatbi/semantic/`、`openchatbi/patrol/`），对上游只做接入点级改动，并在提交信息里说明改了哪个接入点、为什么。
2. **每个提交都要能独立解释**。提交信息写"解决什么失效 / 怎么验证"，而不是"修改了 xx 文件"。
3. **改行为先立测试**。触及已有链路的改动，先补一条锁住旧行为的测试（golden test），再改实现。
4. **数字要能被复现**。文档里写"更准了"不算结论，写清数据集、命令和前后对比。

## 本地校验

```bash
uv run black --check .
uv run ruff check .
uv run pytest
uv run --extra eval runledger run evals/runledger --mode replay --baseline baselines/runledger-baseline.json
```
