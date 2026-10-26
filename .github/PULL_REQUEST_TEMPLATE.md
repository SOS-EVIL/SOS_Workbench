# Pull Request

## 做了什么

## 为什么

## 测试

- [ ] `python test/conformance.py`
- [ ] `python test/integration_phase2.py`
- [ ] `python test/inheritance.py`
- [ ] `python test/cli_smoke.py`
- [ ] `python test/cli_advanced.py`
- [ ] GUI 启动冒烟（如涉及 `app.py` / `ui/`）

## 检查

- [ ] 没有提交 `.sos_*`、`storage/`、`.idea/`、`__pycache__/`
- [ ] 默认安全值未被降低（`ENABLE_MODEL_ACTIONS` / `ENABLE_AUTO_MODE` / `ENABLE_SHELL_ACTION`）
- [ ] 文档与实现一致
- [ ] 没有引入内置领域工具或第二套记忆
