"""
test/cli_smoke.py - CLI 端到端冒烟（无 GUI、无外网）

覆盖：
  1. tools list --json
  2. plan validate / plan submit
  3. run：工具计划执行、inherit_from、run.json、round summary
  4. runs list / show / logs / bundle
  5. stdout 只输出 JSON（不混入 Agent 日志）

运行： python test/cli_smoke.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli.py"
RESULTS = []

def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   << " + str(detail)) if (detail and not ok) else ""))

ECHO = '''
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--target", default="")
p.add_argument("--config", default="{}")
a = p.parse_args()
cfg = json.loads(a.config or "{}")
print("CONFIG=" + json.dumps(cfg, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
print("__JSON_RESULT_START__")
print(json.dumps({"success": True, "echo": cfg}, ensure_ascii=False))
print("__JSON_RESULT_END__")
'''

FAIL = '''
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--target", default="")
p.add_argument("--config", default="{}")
p.parse_args()
print("__ERROR__:boom", file=sys.stderr, flush=True)
print("__JSON_RESULT_START__")
print(json.dumps({"success": False, "error": "boom"}))
print("__JSON_RESULT_END__")
sys.exit(1)
'''

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

def parse_json(proc, label):
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        check(f"{label} 输出为纯 JSON", False, f"{e}: stdout={proc.stdout[:300]!r} stderr={proc.stderr[:300]!r}")
        return None

def main():
    tmp = Path(tempfile.mkdtemp(prefix="sos_cli_test_"))
    tools = tmp / "tools"
    project = tmp / "project"
    data = tmp / "data"
    tools.mkdir()
    project.mkdir()
    plan_path = tmp / "plan.json"
    (tools / "echo_cli.py").write_text(ECHO, encoding="utf-8")
    plan_path.write_text(json.dumps({
        "tools": [
            {"name": "echo_cli", "type": "tool", "params": {"value": "hello"}},
            {"name": "echo_cli", "type": "tool", "params": {}, "inherit_from": "echo_cli"},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    try:
        proc = run_cli(["tools", "list", "--json", "--tool-dir", str(tools)], data)
        payload = parse_json(proc, "tools list")
        check("tools list 退出码 0", proc.returncode == 0, proc.stderr)
        check("tools list 发现临时工具",
              payload and any(t["name"] == "echo_cli" for t in payload.get("tools", [])),
              payload)

        proc = run_cli(["plan", "validate", "--plan", str(plan_path), "--json",
                        "--tool-dir", str(tools)], data)
        payload = parse_json(proc, "plan validate")
        check("plan validate 通过", proc.returncode == 0 and payload and payload.get("steps") == 2,
              (proc.returncode, payload, proc.stderr))

        proc = run_cli(["plan", "submit", "--plan", str(plan_path), "--task-name", "cli_demo",
                        "--json", "--tool-dir", str(tools)], data)
        payload = parse_json(proc, "plan submit")
        check("plan submit 写出计划", proc.returncode == 0 and payload
              and Path(payload.get("plan_path", "")).is_file(), (proc.returncode, payload, proc.stderr))

        proc = run_cli(["run", "--plan", str(plan_path), "--project-dir", str(project),
                        "--target", "demo", "--tool-dir", str(tools), "--no-skills", "--json"], data)
        result = parse_json(proc, "run")
        check("run 退出码 0", proc.returncode == 0, (proc.returncode, proc.stderr[-500:]))
        check("run 状态 success", result and result.get("status") == "success",
              (result.get("status") if result else None, proc.stderr[-500:]))
        check("stdout 未混入 Agent 日志", "[Agent]" not in proc.stdout, proc.stdout[:200])
        check("run.json 已落盘", result and Path(result.get("run_path", "")).is_file(),
              result.get("run_path") if result else None)
        check("round summary 已落盘", result and Path(result.get("summary_path", "")).is_file(),
              result.get("summary_path") if result else None)
        check("inherit_from 已生效",
              result and result.get("steps", {}).get("echo_cli", [{}])[-1]
              .get("result", {}).get("echo", {}).get("inherit_data", {}).get("success") is True,
              result.get("steps") if result else None)
        check("session.log 已生成", result and Path(result.get("session_log", "")).is_file(),
              result.get("session_log") if result else None)

        if result:
            run_id = result["run_id"]
            proc = run_cli(["runs", "show", "--run-id", run_id, "--json"], data)
            payload = parse_json(proc, "runs show")
            check("runs show 可读", proc.returncode == 0 and payload and payload["run"]["run_id"] == run_id,
                  (proc.returncode, proc.stderr))

            proc = run_cli(["runs", "logs", "--run-id", run_id, "--tail", "5", "--json"], data)
            payload = parse_json(proc, "runs logs")
            check("runs logs 可读", proc.returncode == 0 and payload and payload.get("lines"),
                  (proc.returncode, payload, proc.stderr))

            bundle_path = tmp / "bundle.json"
            proc = run_cli(["runs", "bundle", "--run-id", run_id, "--out", str(bundle_path), "--json"], data)
            payload = parse_json(proc, "runs bundle")
            check("runs bundle 退出码 0", proc.returncode == 0, (proc.returncode, proc.stderr[-500:]))
            check("run bundle 文件存在", bundle_path.is_file(), bundle_path)
            if bundle_path.is_file():
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                check("bundle schema 正确", bundle.get("schema") == "sos.run_bundle/v1", bundle.get("schema"))
                check("bundle 含 artifacts", len(bundle.get("artifacts", [])) >= 3,
                      len(bundle.get("artifacts", [])))

        # 非法计划应可预期失败，而不是 traceback
        bad = tmp / "bad.json"
        bad.write_text('{"tools": [{"name": "no_such_tool"}]}', encoding="utf-8")
        proc = run_cli(["plan", "validate", "--plan", str(bad), "--json",
                        "--tool-dir", str(tools)], data)
        payload = parse_json(proc, "bad plan")
        check("非法计划返回结构化错误", proc.returncode == 2 and payload
              and payload.get("code") == "tool_not_found", (proc.returncode, payload, proc.stderr))

        # 执行失败也应返回结构化结果，而不是 traceback
        (tools / "fail_cli.py").write_text(FAIL, encoding="utf-8")
        fail_plan = tmp / "fail.json"
        fail_plan.write_text('{"tools": [{"name": "fail_cli", "params": {}}]}', encoding="utf-8")
        proc = run_cli(["run", "--plan", str(fail_plan), "--project-dir", str(project),
                        "--target", "demo", "--tool-dir", str(tools), "--no-skills", "--json"], data)
        result = parse_json(proc, "failed run")
        check("执行失败返回 3", proc.returncode == 3, (proc.returncode, proc.stderr[-300:]))
        check("执行失败写 run.json", result and Path(result.get("run_path", "")).is_file(),
              (result.get("run_path") if result else None, proc.stderr[-300:]))
        check("执行失败状态 failed", result and result.get("status") == "failed",
              (result.get("status") if result else None, proc.stderr[-300:]))
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
