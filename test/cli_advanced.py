"""
test/cli_advanced.py - CLI 异步、护栏、证据 v2、入库流水线

覆盖：
  1. scope target / max_steps
  2. destructive lock + 审批记录 + actor
  3. evidence_chain / evidence_root / runs verify
  4. run --detach + runs show + runs cancel
  5. runs logs --follow
  6. tools stage / approve / managed tool 执行
  7. skills stage / approve / managed skill 执行

运行： python test/cli_advanced.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli.py"
RESULTS = []

def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   << " + str(detail)) if (detail and not ok) else ""))

def run_cli(args, data_dir, env_extra=None):
    env = os.environ.copy()
    env["SOS_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(data_dir)] + args,
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=120,
    )

def as_json(proc):
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

ECHO = '''
import argparse, json, sys
p=argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--target", default="")
p.add_argument("--config", default="{}")
p.parse_args()
print("__JSON_RESULT_START__")
print(json.dumps({"success": True}))
print("__JSON_RESULT_END__")
'''

SLEEP = '''
import argparse, json, sys, time
p=argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--target", default="")
p.add_argument("--config", default="{}")
p.parse_args()
for i in range(6):
    print(f"heartbeat {i}", file=sys.stderr, flush=True)
    time.sleep(0.5)
print("__JSON_RESULT_START__")
print(json.dumps({"success": True}))
print("__JSON_RESULT_END__")
'''

SKILL = '''
from core.skill.base_skill import BaseSkill

class HelloSkill(BaseSkill):
    name = "hello_skill"
    description = "advanced test skill"
    category = "phase3"

    def execute(self, **kwargs):
        host = kwargs.get("host") or self.current_host or "unknown"
        if self.toolbox:
            self.log_start(host)
            self.log_success(host, "ok")
        return "ok"
'''

def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def wait_status(proc, data_dir, run_id, wanted, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = run_cli(["runs", "show", "--run-id", run_id, "--json"], data_dir)
        payload = as_json(p)
        status = (payload or {}).get("run", {}).get("status")
        if status in wanted:
            return payload["run"]
        time.sleep(0.3)
    return {}

def main():
    tmp = Path(tempfile.mkdtemp(prefix="sos_cli_adv_"))
    tools = tmp / "tools"
    project = tmp / "project"
    data = tmp / "data"
    tools.mkdir()
    project.mkdir()
    write(tools / "echo_tool.py", ECHO)
    write(tools / "sleep_tool.py", SLEEP)
    write(tools / "destructive_tool.py", ECHO)
    write(tools / "destructive_tool.json",
          '{"risk":"destructive","version":"1.0.0","args":["--project-dir","--target","--config"]}')
    write(tmp / "echo_plan.json", '{"tools":[{"name":"echo_tool","params":{}}]}')
    write(tmp / "two_plan.json", '{"tools":[{"name":"echo_tool","params":{}},{"name":"echo_tool","params":{}}]}')
    write(tmp / "destructive_plan.json", '{"tools":[{"name":"destructive_tool","params":{}}]}')
    write(tmp / "sleep_plan.json", '{"tools":[{"name":"sleep_tool","params":{}}]}')
    write(tmp / "scope_deny.json", '{"targets":["other"],"tools":["echo_tool"]}')
    write(tmp / "scope_steps.json", '{"targets":["demo"],"tools":["echo_tool"],"max_steps":1}')

    try:
        # 1. scope
        p = run_cli(["run", "--plan", str(tmp / "echo_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--tool-dir", str(tools), "--scope", str(tmp / "scope_deny.json"), "--json"], data)
        payload = as_json(p)
        check("scope 拒绝未授权 target", p.returncode == 2 and payload
              and payload.get("code") == "scope_denied", (p.returncode, payload, p.stderr[-300:]))

        p = run_cli(["run", "--plan", str(tmp / "two_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--tool-dir", str(tools), "--scope", str(tmp / "scope_steps.json"), "--json"], data)
        payload = as_json(p)
        check("scope 拒绝超出 max_steps", p.returncode == 2 and payload
              and payload.get("code") == "budget_exceeded", (p.returncode, payload, p.stderr[-300:]))

        # 2. destructive lock + approval
        p = run_cli(["run", "--plan", str(tmp / "destructive_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--tool-dir", str(tools), "--json"], data)
        payload = as_json(p)
        check("破坏性工具默认锁定", p.returncode == 2 and payload
              and payload.get("code") == "destructive_locked", (p.returncode, payload, p.stderr[-300:]))

        p = run_cli(["run", "--plan", str(tmp / "destructive_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--tool-dir", str(tools),
                     "--allow-destructive", "--approved-by", "alice", "--actor", "dsh", "--json"], data)
        run = as_json(p)
        check("破坏性工具审批后可执行", p.returncode == 0 and run and run.get("status") == "success",
              (p.returncode, run, p.stderr[-300:]))
        if run:
            kinds = {a.get("kind") for a in run.get("approvals", [])}
            check("审批记录含 destructive", {"run", "destructive"} <= kinds, run.get("approvals"))
            check("actor 记录为 dsh", run.get("actor") == "dsh", run.get("actor"))
            check("证据链已生成", bool(run.get("evidence_chain")) and bool(run.get("evidence_root")),
                  (len(run.get("evidence_chain", [])), run.get("evidence_root")))
            check("工具版本冻结", run.get("tool_frozen") is True, run.get("tool_frozen"))
            v = run_cli(["runs", "verify", "--run-id", run["run_id"], "--json"], data)
            verify = as_json(v)
            check("runs verify 通过", v.returncode == 0 and verify and verify.get("ok") is True,
                  (v.returncode, verify, v.stderr[-300:]))

        # 3. async detach + cancel
        p = run_cli(["run", "--plan", str(tmp / "sleep_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--tool-dir", str(tools), "--no-skills", "--detach", "--json"], data)
        detached = as_json(p)
        check("detach 返回 run_id", p.returncode == 0 and detached and detached.get("run_id"),
              (p.returncode, detached, p.stderr[-300:]))
        if detached:
            run_id = detached["run_id"]
            info = wait_status(p, data, run_id, {"running"}, timeout=5)
            check("detach 后 run 进入 running", info.get("status") == "running", info)
            p = run_cli(["runs", "cancel", "--run-id", run_id, "--by", "human", "--wait", "10", "--json"], data)
            cancelled = as_json(p)
            check("runs cancel 返回 cancelled", p.returncode == 0 and cancelled
                  and cancelled.get("status") == "cancelled", (p.returncode, cancelled, p.stderr[-300:]))
            info = wait_status(p, data, run_id, {"cancelled", "failed", "success"}, timeout=5)
            check("cancel 后 run 为终态", info.get("status") == "cancelled", info)
            v = run_cli(["runs", "verify", "--run-id", run_id, "--json"], data)
            check("取消后的证据链可校验", v.returncode == 0, (v.returncode, v.stderr[-300:]))

        # 4. follow（短任务，等待终态）
        p = run_cli(["run", "--plan", str(tmp / "sleep_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--tool-dir", str(tools), "--no-skills", "--detach", "--json"], data)
        detached = as_json(p)
        if detached:
            run_id = detached["run_id"]
            follow = run_cli(["runs", "logs", "--run-id", run_id, "--follow", "--tail", "0"], data)
            check("runs logs --follow 到达终态", follow.returncode == 0 and "heartbeat" in follow.stdout,
                  (follow.returncode, follow.stdout[:200], follow.stderr[-300:]))

        # 5. tool staging + managed tool
        write(tmp / "staged_tool.py", ECHO)
        p = run_cli(["tools", "stage", "--path", str(tmp / "staged_tool.py"), "--name", "staged_tool",
                     "--risk", "read_only", "--json"], data)
        check("tools stage 成功", p.returncode == 0, (p.returncode, p.stderr[-300:]))
        p = run_cli(["tools", "approve", "--name", "staged_tool", "--by", "tester", "--json"], data)
        payload = as_json(p)
        check("approve 需要 dry-run 确认", p.returncode == 2 and payload
              and payload.get("code") == "approval_required", (p.returncode, payload))
        p = run_cli(["tools", "approve", "--name", "staged_tool", "--by", "tester",
                     "--dry-run-confirmed", "--json"], data)
        check("tools approve 成功", p.returncode == 0, (p.returncode, p.stderr[-300:]))
        p = run_cli(["tools", "list", "--json"], data)
        listed = as_json(p)
        managed = [t for t in (listed or {}).get("tools", []) if t.get("name") == "staged_tool"]
        check("托管工具被发现", bool(managed) and managed[0].get("risk") == "read_only", managed)
        write(tmp / "managed_plan.json", '{"tools":[{"name":"staged_tool","params":{}}]}')
        p = run_cli(["run", "--plan", str(tmp / "managed_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--no-skills", "--json"], data)
        run = as_json(p)
        check("托管工具可执行", p.returncode == 0 and run and run.get("status") == "success",
              (p.returncode, run, p.stderr[-300:]))

        # 6. skill staging + managed skill
        write(tmp / "hello_skill.py", SKILL)
        p = run_cli(["skills", "stage", "--path", str(tmp / "hello_skill.py"), "--name", "hello_skill",
                     "--category", "phase3", "--json"], data)
        check("skills stage 成功", p.returncode == 0, (p.returncode, p.stderr[-300:]))
        p = run_cli(["skills", "approve", "--name", "hello_skill", "--by", "tester",
                     "--dry-run-confirmed", "--json"], data)
        check("skills approve 成功", p.returncode == 0, (p.returncode, p.stderr[-300:]))
        write(tmp / "skill_plan.json", '{"tools":[{"name":"hello_skill","type":"skill","params":{}}]}')
        p = run_cli(["run", "--plan", str(tmp / "skill_plan.json"), "--project-dir", str(project),
                     "--target", "demo", "--json"], data)
        run = as_json(p)
        check("托管 skill 可执行", p.returncode == 0 and run and run.get("status") == "success",
              (p.returncode, run, p.stderr[-300:]))
        if run:
            check("skill hash 已记录", bool(run.get("skill_versions", {}).get("hello_skill")),
                  run.get("skill_versions"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 58)
    print(f"结果：{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}   {detail}")
    print("=" * 58)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
