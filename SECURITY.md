# Security Policy

## Supported versions

SOS_Workbench 目前处于 0.1.x 预览阶段，只对最新提交提供安全修复。

## Reporting a vulnerability

请使用 GitHub 的 **Private vulnerability reporting / Security advisory** 提交，不要在公开 issue 中贴出：

- API Key、token、密码或任何真实凭据
- 可被利用的完整命令
- 含本地路径或客户数据的日志

如果没有私有报告入口，请先开一个不含敏感细节的 issue，说明“需要私下联系”。

## 安全边界

SOS 是一个**单机、单用户、本地优先**的桌面工具，不是服务端组件，也不提供沙箱。

- `external_tools/`、`skills/`、`{SOS_DATA_DIR}/tools`、`{SOS_DATA_DIR}/skills` 中的代码会以**当前用户权限**执行；只加载可信来源。
- 工具子进程不会收到平台注入的 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`，但仍会继承用户环境里的其他变量；不要在不可信工具旁边放置长期凭据。
- `{"$secret": "ENV"}` 只保证 SOS 自己的日志/快照使用脱敏副本；工具自己的 stderr 仍可能输出敏感内容。
- 模型动作（`ENABLE_MODEL_ACTIONS`）、自动模式（`ENABLE_AUTO_MODE`）和 `shell_exec`（`ENABLE_SHELL_ACTION`）默认关闭。打开它们会扩大攻击面。
- 项目没有多租户隔离、鉴权、网络边界或审计签名；不要把它暴露成共享服务。
- CLI 的 `run` 没有交互式确认；高风险动作的审批必须放在 DSH/人这一层。
- 建议 CLI 运行使用 `--scope` 限制 target/tools/budget；破坏性工具需要 `--allow-destructive` + `--approved-by`。
- 由 DSH 生成的工具/skill 应先 `stage`、静态检查、只读 dry-run，再由人 `approve --by`；运行期会检查 tool/skill hash 是否变化。
- `SOS_EVIDENCE_KEY` 设置后，run 记录会带 HMAC 签名；`runs verify` 可校验证据链与签名。

## 不属于漏洞的情况

- 使用者主动加载恶意工具或技能。
- 使用者把 API Key 写进步骤参数、工具输出或自己的日志。
- 使用者主动打开默认关闭的自动执行开关。
- 在没有授权的目标上使用接入的工具。

这些属于使用者的信任与合规责任；SOS 只负责执行与留痕。
