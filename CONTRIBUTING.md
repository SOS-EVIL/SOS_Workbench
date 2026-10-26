# Contributing

SOS_Workbench 的定位是 **DSH 下游的可视化执行工作台**：不提供记忆、不做自主规划、不内置领域工具。

## 开发环境

```bash
python --version            # 3.10+
pip install -r requirements.txt
python main.py              # 启动 GUI
```

## 测试

```bash
python test/conformance.py
python test/integration_phase2.py
python test/inheritance.py
python test/cli_smoke.py
python test/cli_advanced.py
```

五个测试都不需要 GUI、不需要外网；提交前请全部跑通。

## 改代码前

- 先确认改动属于 **执行 / 可视化 / 证据** 层；不要在本仓库里加领域工具或第二套记忆。
- 保持默认安全值：`ENABLE_MODEL_ACTIONS`、`ENABLE_AUTO_MODE`、`ENABLE_SHELL_ACTION` 都为 `False`。
- 新增/修改工具契约时，同步更新 [对齐方法.txt](对齐方法.txt)。
- 新增/修改日志或轮次产物时，同步更新 [日志结构.txt](日志结构.txt)。
- 新增/修改递归嵌套行为时，同步更新 [递归标准.txt](递归标准.txt)。

## 提交清单

- [ ] 只做一件事，提交信息能说明“为什么”
- [ ] 新行为有对应测试（优先加在 `test/`）
- [ ] 没有把 `.sos_*`、`storage/`、`.idea/`、`__pycache__/` 带进提交
- [ ] 没有降低默认安全值，或已在 PR 中说明理由
- [ ] 文档与实现一致；不确定的写成“待验证”，不要写成既成事实
- [ ] 没有引入新的领域专用逻辑或内置攻击能力

## 报告问题

- 安全相关问题请走 [SECURITY.md](SECURITY.md)，不要在公开 issue 贴凭据。
- 功能/兼容性问题请说明：Python 版本、Windows 版本、目标类型、工具名、复现步骤、相关日志（脱敏后）。
