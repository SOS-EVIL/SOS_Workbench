# CLI

`cli.py` 是 SOS_Workbench 的无 GUI 入口，给 DSH（或任何脚本）调用。

设计原则：

- **一次调用只做一件事**：校验、提交、执行、取消、查证据。
- **stdout 只输出 JSON**（`--json`）；日志、加载提示走 stderr。
- **退出码稳定**：`0` 成功，`2` 参数/计划/scope 错误，`3` 执行失败，`130` 中断。
- **复用现有 core**：CLI 通过 `core/headless.py` 调用 `Phase2Freedom`，不另写执行逻辑。

---

## 运行

```bash
python cli.py --help
python cli.py tools list --json
python cli.py plan validate --plan plan.json --json
python cli.py run --plan plan.json --project demo --target example.com --json
```

Windows 下也可以用 `sos_workbench.cmd`：

```bat
sos_workbench.cmd tools list --json
```

---

## 数据目录

默认运行数据在 `storage/`。可以用环境变量或参数隔离：

```bash
set SOS_DATA_DIR=D:\sos-data
python cli.py run --plan plan.json --target demo --json

# 或者
python cli.py --data-dir D:\sos-data run --plan plan.json --target demo --json
```

```text
{SOS_DATA_DIR}/
├── projects/{项目}/
│   ├── assets.json
│   ├── session.log
│   ├── structured_logs/
│   └── latest/
├── round/{任务}/{任务}_round_N.json
├── phase3/
├── runs/{run_id}/
│   ├── spec.json
│   ├── plan.json
│   ├── run.json
│   ├── worker.log        （--detach 时）
│   └── cancel            （runs cancel 时）
├── staging/{tool,skill}/{name}/manifest.json
├── tools/                审核通过的托管工具
└── skills/{category}/    审核通过的托管技能
```

---

## 命令

### tools

```bash
python cli.py tools list --json
python cli.py tools list --json --tool-dir D:\my-tools
python cli.py tools validate --path external_tools\sqlite_query.py --json
```

`tools list` 返回工具名、路径、声明的长参数、`risk`、`version`、是否接受 `--config`。

### plan

```bash
python cli.py plan validate --plan plan.json --json
python cli.py plan submit --plan plan.json --task-name demo --json
```

`plan validate` 会检查：

- `tools` 数组非空；
- 每步有 `name`，`type` 是 `tool` 或 `skill`；
- `params` 是合法 JSON 对象；
- `inherit_from` 只能引用之前的步骤；
- `inherit_file` 必须存在；
- `tool` 步骤的工具必须已注册。

`plan submit` 把计划写到 `{SOS_DATA_DIR}/round/{task_name}/plan.json`，并写 `task_config.json`。

### run

同步：

```bash
python cli.py run \
  --plan plan.json \
  --project demo \
  --target example.com \
  --tool-dir D:\my-tools \
  --timeout 300 \
  --json
```

异步：

```bash
python cli.py run --plan plan.json --project demo --target example.com --detach --json
# → { "run_id": "...", "status": "running", "pid": 1234 }

python cli.py runs show --run-id <id> --json
python cli.py runs logs --run-id <id> --follow
python cli.py runs cancel --run-id <id> --by human
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--project` | 项目名，默认 `cli`，目录为 `projects/{project}` |
| `--project-dir` | 直接指定项目目录，优先于 `--project` |
| `--target` | 本次 run 的目标（不透明字符串） |
| `--task-name` | 任务名，默认取计划文件名 |
| `--round` | 轮次，默认 1 |
| `--timeout` | 整次 run 的超时秒数，0 表示不限 |
| `--tool-dir` | 额外工具目录，可重复 |
| `--no-skills` | 不加载 skills/，只跑工具步骤 |
| `--detach` | 后台 worker 执行，立即返回 run_id |
| `--scope` | scope/allowlist JSON |
| `--actor` | `human` / `dsh` / `sos-reviewer`，默认 `human` |
| `--approved-by` | 审批人；破坏性动作必填 |
| `--approval-note` | 审批备注 |
| `--allow-destructive` | 解锁破坏性工具 |
| `--require-manifest` | 工具必须有 manifest/risk |
| `--max-steps` | 最大步骤数 |
| `--max-duration` | 最大时长（秒） |
| `--parent-run-id` | 二次编排的父 run |
| `--plan-version` | 计划版本，默认 `1` |
| `--model-id` / `--model-prompt-hash` | 本次计划使用的模型与 prompt 版本 |
| `--json` | stdout 只输出 JSON |

同步 `run` 返回时，`run.json`、`round_N.json`、`session.log` 已落盘。成功返回 `0`，失败返回 `3`。

返回的 run 记录包含：

```json
{
  "schema": "sos.run/v1",
  "run_id": "...",
  "status": "success",
  "mode": "sync",
  "pid": 1234,
  "task_name": "demo",
  "round": 1,
  "target": "example.com",
  "plan": { "tools": [] },
  "plan_hash": "...",
  "plan_version": "1",
  "parent_run_id": null,
  "actor": "human",
  "scope": {},
  "scope_hash": "",
  "budget": {},
  "approvals": [],
  "model_id": "",
  "model_prompt_hash": "",
  "steps": {},
  "tool_versions_start": {},
  "tool_versions": {},
  "tool_frozen": true,
  "skill_versions": {},
  "skill_frozen": true,
  "evidence_chain": [],
  "evidence_root": "...",
  "hash_algorithm": "sha256",
  "signature": null,
  "summary_path": "...",
  "session_log": "...",
  "structured_logs": "..."
}
```

---

## scope / 高风险护栏

scope 是一个 JSON 文件：

```json
{
  "targets": ["demo", "example.com"],
  "tools": ["sqlite_query", "http_probe"],
  "allow_destructive": false,
  "require_manifest": true,
  "max_steps": 20,
  "max_duration": 600,
  "max_requests": 1000
}
```

```bash
python cli.py run --plan plan.json --target example.com --scope scope.json --json
```

行为：

- `targets` 不匹配 → `scope_denied`。
- `tools` 不匹配 → `scope_denied`。
- 步骤数超过 `max_steps` → `budget_exceeded`。
- 时长超过 `max_duration` → 超时取消。
- 工具 manifest 的 `risk == "destructive"` 且没有 `--allow-destructive` → `destructive_locked`。
- 破坏性动作必须再传 `--approved-by`，写入 `approvals`。
- `require_manifest` 下 `risk` 未知 → `scope_denied`。
- `max_requests` 当前只记录在 `budget`，由工具自己遵守，CLI 不做请求计数。

---

## 入库流水线

工具：

```bash
python cli.py tools stage --path D:\gen\my_tool.py --name my_tool --risk read_only --json
python cli.py tools staged --json

# 人在只读目标上 dry-run 后
python cli.py tools approve --name my_tool --by alice --dry-run-confirmed --json
python cli.py tools reject --name my_tool --by alice --note "..." --json
```

技能：

```bash
python cli.py skills stage --path D:\gen\my_skill.py --name my_skill --category phase3 --json
python cli.py skills approve --name my_skill --by alice --dry-run-confirmed --json
```

流程：

```text
stage（复制到 staging/ + 编译 + AST 静态检查）
  → 人用只读目标 dry-run
  → approve --by + --dry-run-confirmed
  → hash/版本写入 manifest
  → 复制到 {SOS_DATA_DIR}/tools 或 {SOS_DATA_DIR}/skills/{category}
```

- 导入期间工具/skill 的 `sha256` 会被记录；run 结束会对比开始/结束 hash，变化则 `tool_frozen/skill_frozen=false` 并判失败。
- `--strict` 会把静态检查的 finding 当成拒绝理由；默认只记录。
- `tools approve` 不接受空 `--by`，也不接受没有 dry-run 确认的审批。

---

## runs

```bash
python cli.py runs list --json
python cli.py runs show --run-id ... --json
python cli.py runs logs --run-id ... --tail 100 --json
python cli.py runs logs --run-id ... --follow [--json]
python cli.py runs cancel --run-id ... --by human --wait 10 --json
python cli.py runs verify --run-id ... --json
python cli.py runs bundle --run-id ... --out bundle.json --json
```

`runs logs --follow` 会持续输出新行，直到 run 进入 `success` / `failed` / `cancelled`。  
`--json` 时每行输出一个 JSON 对象（JSONL），不是单个数组。

`runs verify` 重新计算证据链和 root，并校验：

- `evidence_chain` 是否匹配；
- `evidence_root` 是否匹配；
- 如果设置了 `SOS_EVIDENCE_KEY`，校验 HMAC 签名。

`runs bundle` 生成 `sos.run_bundle/v1`：

```json
{
  "schema": "sos.run_bundle/v1",
  "run": {},
  "round_summary": {},
  "artifacts": [
    {"path": "...", "relative": "...", "sha256": "...", "size": 123}
  ]
}
```

---

## 计划格式

```json
{
  "tools": [
    {
      "name": "sqlite_query",
      "type": "tool",
      "params": "{\"sql\": \"select 1\"}",
      "inherit_from": null,
      "inherit_file": null
    },
    {
      "name": "http_probe",
      "type": "tool",
      "params": {"method": "GET", "path": "/health"},
      "inherit_from": "sqlite_query",
      "inherit_file": null
    }
  ]
}
```

`params` 可以是 JSON 字符串或对象；CLI 会统一规范成字符串写入计划。

---

## 给 DSH 的调用方式

同步：

```text
DSH → 生成 plan.json
DSH → run --plan plan.json --project demo --target example.com --actor dsh --json
DSH → 解析 {run_id, status, summary_path, session_log}
DSH → runs bundle --run-id ... --out bundle.json
DSH → 读 bundle.json 回写记忆/下一步
```

异步：

```text
DSH → run --detach ...
DSH → 拿 run_id
DSH → runs logs --run-id ... --follow --json
DSH → runs show --run-id ... --json
DSH → 必要时 runs cancel --run-id ...
```

---

## 当前限制

- `run --detach` 的 worker 是独立进程；异常退出时 `runs cancel` 会尝试 `taskkill /T` / killpg，但极端情况下仍可能需要手工清理。
- `runs cancel` 的等待上限由 `--wait` 控制；超时后会强杀 worker 并标记 cancelled。
- `skill` 步骤要求技能遵守 `BaseSkill` 生命周期并写终态快照；不遵守的技能会等到超时。
- CLI 不启动 Tk，不做交互式确认；高风险动作请在上层（DSH/人）审批。
- `max_requests` 只记录，不做请求计数；需要工具自己遵守。
- L2 单链步骤默认串行；并发通过多个 run 或多个 target 实现，链内 parallel group 仍在路线图中。
- 仅 Windows 验证；POSIX 下 `ToolRunner` 有对应分支，但未在 CI 覆盖。
