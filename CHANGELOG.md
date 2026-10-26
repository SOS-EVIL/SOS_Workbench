# Changelog

## 0.1.0 — 2026-10-27

### Added

- CLI：`tools list/validate/stage/approve`、`skills stage/approve`、`plan validate/submit`、`run`、`runs list/show/logs/cancel/verify/bundle`（[docs/CLI.md](docs/CLI.md)）
- 异步执行：`run --detach`、`runs cancel`、`runs logs --follow`
- 高风险护栏：scope/allowlist、`max_steps`/`max_duration`、破坏性动作锁、`--approved-by`、`actor` 记录
- 证据 v2：`parent_run_id`、`plan_version`、`model_id`/`model_prompt_hash`、tool/skill hash、审批记录、hash chain、可选 HMAC 签名、`runs verify`
- 入库流水线：`staging/` → 静态检查 → 人工确认 dry-run → `--by` 审批 → hash/版本注册 → 托管 `tools/` / `skills/`
- `core/headless.py`：无 GUI 的 Phase2 宿主，CLI 与测试复用现有执行核
- `SOS_DATA_DIR` / `--data-dir`：运行数据目录可隔离
- `sos.run/v1` 与 `sos.run_bundle/v1`：run 记录与证据包
- `test/cli_smoke.py`、`test/cli_advanced.py`：CLI 端到端与异步/护栏/入库测试

### Changed

- 项目全面更名为 **SOS_Workbench**（Python 类、窗口标题、CLI 包装器、文档、CI 引用）
- README 重写为 **DSH 下游的可视化执行工作台**（SOS_Workbench），明确“记忆在 DSH，SOS 只留执行证据”
- CLI 模式下 `--json` 的 stdout 只输出 JSON；Agent/结构化日志提示改走 stderr
- 模型动作默认关闭（`ENABLE_MODEL_ACTIONS = False`）：模型只给建议，执行由人触发
- 「鲸吞/栖鲸」自动模式默认关闭（`config/tools.py` → `ENABLE_AUTO_MODE = False`）
- `storage/` 目录改为应用启动时创建，不再在 import `config.tools` 时写盘
- 工具子进程不再继承平台注入的 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`

### Fixed

- `inherit_from` 现在会把每一步结果写回 `task_data`，后续步骤能收到上一步的结果对象
- `_wait_skill_complete` 按 `exec_index` 监视快照，避免同一技能第二次执行读到旧终态
- `chat_bot` 非阻塞模式补齐 `BaseSkill` 生命周期，可在 CLI/L2 中快速结束
- 新增 `test/inheritance.py`，CI 同步执行

### Docs

- README 移除旧版截图引用；含本地路径的旧截图已从版本库移除
- 新增 `config/version.py` 单一版本来源、`docs/RELEASE.md`、tag 发布 workflow
- 新增最大化窗口截图 `docs/screenshot-exec.png` 与 `docs/screenshot-chain.png`
- 修正“测试会写 storage”“默认发行版不含 skills”等文档漂移

## 0.0.1 — 2026-10-26

初始原型（未公开发布）。

### 内核

- `target` 抽象：域名 / DSN / 本地路径 / 任意字符串统一处理，不做语义解释
- 目录名（slug）派生：纯域名与历史规则逐字符兼容，其它 target 追加哈希后缀
- 工具发现：扫描 `tools/` 与 `external_tools/`，按后缀启动（py/pyz/exe/bat/cmd/ps1/sh/js）
- 参数能力探测：按工具声明下传 `--target` / `--host` / `--config`，旧工具不受影响
- 参数同名映射：未声明 `--config` 的工具，`params` 中同名的键映射为命令行参数
- 调度：并发闸门（界面并发数为真实上限）、`run_id` 精确终止、单步超时（默认 900s）
- 进程管理：流式日志、暂停、取消、**进程树**强杀（Windows `taskkill /T`）
- 凭证：`{"$secret": "ENV_NAME"}` 运行时解析；日志、快照、模型上下文脱敏
- 日志：界面实时 / `session.log` 异步有界缓冲 / `structured_logs` JSONL
- 递归嵌套：L2 步骤可挂起等待 L3 子任务，带重复登记保护与等待超时

### 界面

- 三个页签：执行台 / 编排台 / 决策台
- 模型选择：`DEEPSEEK-V4.1FLASH`（默认）与 `DEEPSEEK-V4-PRO`，选择持久化
- 添加目标对话框接受任意字符串，并给出 5 类示例

### 安全默认值

- `shell_exec` 默认关闭（`ENABLE_SHELL_ACTION = False`）
- 发行版不含任何领域工具；示例工具为只读
- `.sos_*`、`storage/`、`.idea/` 不进入版本库

### 文档与工程

- README、项目结构、对齐方法、递归标准、轮次 skill 规则、日志结构
- `test/conformance.py`、`test/integration_phase2.py`、`test/inheritance.py`
- GitHub Actions（Windows / Python 3.10、3.12）、`.gitattributes`、MIT License

### 已知限制

- 单机、单用户、无鉴权，不是服务端组件
- Skill 实例为单例，不能被多个目标并发调用
- 递归嵌套尚无深度上限与环检测（见 `递归标准.txt` G1–G6）
- `storage/` 为文件存储，无查询与断点续跑
