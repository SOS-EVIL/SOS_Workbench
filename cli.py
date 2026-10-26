#!/usr/bin/env python3
"""cli.py - SOS_Workbench 的无 GUI 命令行入口。

设计目标：
    - DSH 可以直接调用本命令提交计划、执行、读取证据；
    - stdout 只输出 JSON（--json 时），stderr 留给日志；
    - 复用现有 Phase2Freedom / TaskExecutor / ToolRunner，不另写执行逻辑；
    - 所有 run 记录落到 {SOS_DATA_DIR}/runs/{run_id}/run.json。

常用：
    python cli.py tools list --json
    python cli.py plan validate --plan plan.json --json
    python cli.py run --plan plan.json --project demo --target example.com --json
    python cli.py runs show --run-id <id> --json
    python cli.py runs bundle --run-id <id> --out bundle.json
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from config.version import __version__

VERSION = __version__
ROOT = Path(__file__).resolve().parent


class CLIError(Exception):
    def __init__(self, code, message, exit_code=2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _bootstrap_data_dir(argv):
    """在导入 config.tools 之前设置 SOS_DATA_DIR，保证数据目录可隔离。"""
    for i, arg in enumerate(argv):
        if arg == "--data-dir" and i + 1 < len(argv):
            os.environ["SOS_DATA_DIR"] = argv[i + 1]
        elif arg.startswith("--data-dir="):
            os.environ["SOS_DATA_DIR"] = arg.split("=", 1)[1]


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise CLIError("not_found", f"文件不存在: {path}")
    except json.JSONDecodeError as e:
        raise CLIError("json_invalid", f"JSON 解析失败: {path}: {e}")
    except OSError as e:
        raise CLIError("io_error", f"读取失败: {path}: {e}")


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _hash_obj(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _safe_task_name(name):
    name = (name or "").strip()
    if not name or name in (".", ".."):
        raise CLIError("task_name_invalid", "任务名不能为空")
    if Path(name).name != name or re.search(r'[\\/:*?"<>|]', name):
        raise CLIError("task_name_invalid", f"任务名不能包含路径分隔符: {name}")
    return name


def _safe_run_id(run_id):
    if not run_id or Path(run_id).name != run_id or re.search(r'[\\/:*?"<>|]', run_id):
        raise CLIError("run_id_invalid", f"非法 run_id: {run_id}")
    return run_id


def _tool_dirs(extra):
    from config.tools import TOOL_DIRS

    dirs = list(TOOL_DIRS)
    for item in extra or []:
        p = Path(item).expanduser().resolve()
        if p not in dirs:
            dirs.append(p)
    return dirs


def _registry(extra):
    from core.tool_registry import ToolRegistry

    return ToolRegistry(tool_dirs=_tool_dirs(extra))


def _normalize_plan(plan_path, registry):
    data = _read_json(plan_path)
    if isinstance(data, dict) and isinstance(data.get("plan"), dict):
        data = data["plan"]
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise CLIError("plan_invalid", "计划必须包含 tools 数组")

    tools = data["tools"]
    if not tools:
        raise CLIError("plan_invalid", "计划中没有步骤")

    normalized = []
    seen = set()
    available = registry.get_all_tools()
    for index, step in enumerate(tools, start=1):
        if not isinstance(step, dict):
            raise CLIError("plan_invalid", f"第 {index} 步不是对象")
        name = str(step.get("name") or "").strip()
        if not name:
            raise CLIError("plan_invalid", f"第 {index} 步缺少 name")
        step_type = str(step.get("type") or "tool").strip().lower()
        if step_type not in ("tool", "skill"):
            raise CLIError("plan_invalid", f"第 {index} 步 type 必须是 tool 或 skill")

        raw_params = step.get("params", "{}")
        if isinstance(raw_params, dict):
            params_obj = raw_params
        else:
            try:
                params_obj = json.loads(str(raw_params or "{}"))
            except json.JSONDecodeError as e:
                raise CLIError("plan_invalid", f"第 {index} 步 params 不是合法 JSON: {e}")
        if not isinstance(params_obj, dict):
            raise CLIError("plan_invalid", f"第 {index} 步 params 必须是对象")

        inherit_from = step.get("inherit_from") or None
        if inherit_from is not None:
            inherit_from = str(inherit_from).strip() or None
        if inherit_from and inherit_from not in seen:
            raise CLIError(
                "plan_invalid",
                f"第 {index} 步 inherit_from={inherit_from!r} 不是之前的步骤",
            )

        inherit_file = step.get("inherit_file") or None
        if inherit_file:
            inherit_file = str(inherit_file)
            if not Path(inherit_file).is_file():
                raise CLIError("plan_invalid", f"第 {index} 步 inherit_file 不存在: {inherit_file}")

        if step_type == "tool" and name not in available:
            raise CLIError("tool_not_found", f"第 {index} 步工具未注册: {name}")

        normalized.append({
            "name": name,
            "type": step_type,
            "params": json.dumps(params_obj, ensure_ascii=False),
            "inherit_from": inherit_from,
            "inherit_file": inherit_file,
        })
        seen.add(name)
    return {"tools": normalized}


def _tool_versions(plan, registry):
    versions = {}
    available = registry.get_all_tools()
    for step in plan.get("tools", []):
        name = step["name"]
        path = available.get(name)
        if not path:
            continue
        versions[name] = {"path": path, "sha256": _sha256(path)}
    return versions


def _load_run(run_id):
    from config.tools import RUNS_DIR

    run_id = _safe_run_id(run_id)
    path = RUNS_DIR / run_id / "run.json"
    if not path.exists():
        raise CLIError("run_not_found", f"找不到 run: {run_id}")
    return path, _read_json(path)


def cmd_tools_list(args):
    registry = _registry(args.tool_dir)
    tools = []
    for name, path in sorted(registry.get_all_tools().items()):
        info = registry.get_tool_info(name) or {}
        tools.append({
            "name": name,
            "path": path,
            "args": sorted(registry.args_of(name)),
            "accepts_config": registry.accepts_config(name),
            "description": info.get("description", ""),
            "risk": info.get("risk", "unknown"),
            "version": info.get("version", ""),
        })
    if args.json:
        _print_json({"ok": True, "schema": "sos.tools/v1", "tools": tools})
    else:
        for item in tools:
            print(f"{item['name']}\t{item['path']}")
    return 0


def cmd_tools_validate(args):
    path = Path(args.path).expanduser().resolve()
    if not path.is_file():
        raise CLIError("not_found", f"工具不存在: {path}")
    from config.tools import TOOL_EXTENSIONS

    if path.suffix.lower() not in TOOL_EXTENSIONS:
        raise CLIError("tool_invalid", f"不支持的后缀: {path.suffix}")
    compiled = None
    if path.suffix.lower() == ".py":
        try:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
            compile(source, str(path), "exec")
            compiled = True
        except (OSError, SyntaxError) as e:
            raise CLIError("tool_invalid", f"Python 编译失败: {e}")
    result = {
        "ok": True,
        "schema": "sos.tool_validate/v1",
        "path": str(path),
        "suffix": path.suffix.lower(),
        "compiled": compiled,
        "sha256": _sha256(path),
    }
    if args.json:
        _print_json(result)
    else:
        print(f"ok: {path}")
    return 0


def cmd_plan_validate(args):
    registry = _registry(args.tool_dir)
    plan = _normalize_plan(args.plan, registry)
    result = {
        "ok": True,
        "schema": "sos.plan/v1",
        "steps": len(plan["tools"]),
        "tools": [s["name"] for s in plan["tools"]],
        "plan": plan,
    }
    if args.json:
        _print_json(result)
    else:
        print(f"ok: {len(plan['tools'])} steps")
    return 0


def cmd_plan_submit(args):
    from config.tools import ROUND_BASE_DIR, ensure_data_dirs

    ensure_data_dirs()
    registry = _registry(args.tool_dir)
    plan = _normalize_plan(args.plan, registry)
    task_name = _safe_task_name(args.task_name or Path(args.plan).stem)
    task_dir = ROUND_BASE_DIR / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    plan_path = task_dir / "plan.json"
    config_path = task_dir / "task_config.json"
    _write_json(plan_path, plan)
    _write_json(config_path, {
        "task_name": task_name,
        "current_round": 1,
        "plan": plan,
    })
    result = {
        "ok": True,
        "schema": "sos.plan_submit/v1",
        "task_name": task_name,
        "plan_path": str(plan_path),
        "task_config_path": str(config_path),
        "steps": len(plan["tools"]),
    }
    if args.json:
        _print_json(result)
    else:
        print(f"submitted: {task_name} ({len(plan['tools'])} steps)")
        print(f"plan: {plan_path}")
    return 0


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _run_dir(run_id):
    from config.tools import RUNS_DIR

    return RUNS_DIR / _safe_run_id(run_id)


def _load_scope(path):
    if not path:
        return {}
    data = _read_json(path)
    if not isinstance(data, dict):
        raise CLIError("scope_invalid", "scope 必须是 JSON 对象")
    return data


def _scope_guard(plan, registry, args, target):
    """scope/allowlist、破坏性动作锁、预算与审批记录。"""
    scope = _load_scope(getattr(args, "scope", None))
    targets = scope.get("targets") or ["*"]
    if not isinstance(targets, list):
        raise CLIError("scope_invalid", "scope.targets 必须是数组")
    if "*" not in targets and target not in targets:
        raise CLIError("scope_denied", f"target 不在 scope 内: {target}")

    allowed_tools = scope.get("tools")
    if allowed_tools is not None:
        if not isinstance(allowed_tools, list):
            raise CLIError("scope_invalid", "scope.tools 必须是数组")
        for step in plan["tools"]:
            if step["name"] not in allowed_tools:
                raise CLIError("scope_denied", f"工具不在 scope 内: {step['name']}")

    max_steps = int(scope.get("max_steps") or getattr(args, "max_steps", 0) or 0)
    if max_steps and len(plan["tools"]) > max_steps:
        raise CLIError("budget_exceeded", f"计划步骤 {len(plan['tools'])} 超过 max_steps={max_steps}")

    max_duration = float(scope.get("max_duration") or getattr(args, "max_duration", 0) or 0)
    allow_destructive = bool(scope.get("allow_destructive") or getattr(args, "allow_destructive", False))
    require_manifest = bool(scope.get("require_manifest") or getattr(args, "require_manifest", False))
    approved_by = (getattr(args, "approved_by", "") or "").strip()

    destructive_tools = []
    for step in plan["tools"]:
        if step["type"] != "tool":
            continue
        info = registry.get_tool_info(step["name"]) or {}
        risk = str(info.get("risk") or "unknown").lower()
        if require_manifest and risk == "unknown":
            raise CLIError("scope_denied", f"工具缺少 manifest/risk: {step['name']}")
        if risk == "destructive":
            destructive_tools.append(step["name"])

    if destructive_tools and not allow_destructive:
        raise CLIError(
            "destructive_locked",
            "计划包含破坏性工具，需要 --allow-destructive（或在 scope 中 allow_destructive=true）",
        )
    if destructive_tools and not approved_by:
        raise CLIError("approval_required", "破坏性动作需要 --approved-by 记录审批人")

    budget = {
        "max_steps": max_steps or None,
        "max_duration": max_duration or None,
        "max_requests": scope.get("max_requests"),
        "allow_destructive": allow_destructive,
        "require_manifest": require_manifest,
    }
    approvals = [{
        "kind": "run",
        "actor": getattr(args, "actor", "human"),
        "by": approved_by or getattr(args, "actor", "human"),
        "note": getattr(args, "approval_note", "") or "",
        "at": _now(),
    }]
    if destructive_tools:
        approvals.append({
            "kind": "destructive",
            "actor": getattr(args, "actor", "human"),
            "by": approved_by,
            "note": getattr(args, "approval_note", "") or "",
            "tools": destructive_tools,
            "at": _now(),
        })
    return scope, budget, approvals


def _prepare_run(args):
    from config.tools import PROJECTS_DIR, RUNS_DIR, ensure_data_dirs

    ensure_data_dirs()
    round_num = int(args.round)
    if round_num < 1:
        raise CLIError("round_invalid", "--round 必须 >= 1")
    target = str(args.target).strip()
    if not target:
        raise CLIError("target_invalid", "--target 不能为空")

    registry = _registry(args.tool_dir)
    plan = _normalize_plan(args.plan, registry)
    scope, budget, approvals = _scope_guard(plan, registry, args, target)

    project_dir = (
        Path(args.project_dir).expanduser().resolve()
        if args.project_dir else (PROJECTS_DIR / _safe_task_name(args.project or "cli"))
    )
    project_dir.mkdir(parents=True, exist_ok=True)
    task_name = _safe_task_name(args.task_name or Path(args.plan).stem)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    plan_copy = run_dir / "plan.json"
    _write_json(plan_copy, plan)

    assets_path = project_dir / "assets.json"
    if not assets_path.exists():
        from core import targets as target_api

        _write_json(assets_path, {
            "domain": project_dir.name,
            "timestamp": _now(),
            "total_assets": 1,
            "targets": [{
                "target": target,
                "host": target,
                "kind": target_api.kind_of(target),
                "ip": "",
                "is_alive": True,
                "source": "cli",
            }],
        })

    timeout = float(getattr(args, "timeout", 0) or 0)
    if budget.get("max_duration"):
        timeout = min(timeout, budget["max_duration"]) if timeout else budget["max_duration"]

    spec = {
        "run_id": run_id,
        "mode": "detach" if getattr(args, "detach", False) else "sync",
        "task_name": task_name,
        "round": round_num,
        "project_dir": str(project_dir),
        "target": target,
        "source_plan": str(Path(args.plan).expanduser().resolve()),
        "plan_path": str(plan_copy),
        "plan": plan,
        "plan_hash": _hash_obj(plan),
        "plan_version": str(getattr(args, "plan_version", "") or "1"),
        "parent_run_id": getattr(args, "parent_run_id", None) or None,
        "actor": getattr(args, "actor", "human"),
        "scope": scope,
        "scope_hash": _hash_obj(scope) if scope else "",
        "budget": budget,
        "approvals": approvals,
        "model_id": getattr(args, "model_id", "") or "",
        "model_prompt_hash": getattr(args, "model_prompt_hash", "") or "",
        "tool_dirs": [str(p) for p in _tool_dirs(args.tool_dir)],
        "no_skills": bool(getattr(args, "no_skills", False)),
        "timeout": timeout or 0,
    }
    _write_json(run_dir / "spec.json", spec)
    return registry, plan, project_dir, task_name, run_id, spec


def _base_record(spec, run_id, status="queued"):
    project_dir = Path(spec["project_dir"])
    from config.tools import ROUND_BASE_DIR

    summary_path = ROUND_BASE_DIR / spec["task_name"] / f"{spec['task_name']}_round_{spec['round']}.json"
    return {
        "schema": "sos.run/v1",
        "run_id": run_id,
        "status": status,
        "success": False,
        "mode": spec.get("mode", "sync"),
        "pid": None,
        "task_name": spec["task_name"],
        "round": spec["round"],
        "project_dir": str(project_dir),
        "target": spec["target"],
        "plan_path": spec["plan_path"],
        "source_plan": spec.get("source_plan"),
        "plan": spec["plan"],
        "plan_hash": spec["plan_hash"],
        "plan_version": spec.get("plan_version", "1"),
        "parent_run_id": spec.get("parent_run_id"),
        "actor": spec.get("actor", "human"),
        "scope": spec.get("scope", {}),
        "scope_hash": spec.get("scope_hash", ""),
        "budget": spec.get("budget", {}),
        "approvals": spec.get("approvals", []),
        "model_id": spec.get("model_id", ""),
        "model_prompt_hash": spec.get("model_prompt_hash", ""),
        "started_at": _now(),
        "finished_at": None,
        "duration": 0,
        "exit_code": None,
        "steps": {},
        "summary_path": str(summary_path),
        "session_log": str(project_dir / "session.log"),
        "structured_logs": str(project_dir / "structured_logs"),
        "tool_versions": _tool_versions(spec["plan"], _registry(spec["tool_dirs"])),
        "skill_versions": {},
        "log_tail": [],
        "error": None,
        "cancel_requested": False,
        "evidence_chain": [],
        "evidence_root": "",
        "hash_algorithm": "sha256",
        "signature": None,
    }


def _build_evidence_chain(steps):
    chain = []
    prev = ""
    seq = 0
    for tool_name, results in (steps or {}).items():
        if not isinstance(results, list):
            continue
        for result in results:
            seq += 1
            payload = {"seq": seq, "tool": tool_name, "result": result, "prev": prev}
            digest = _hash_obj(payload)
            chain.append({"seq": seq, "tool": tool_name, "prev": prev, "hash": digest})
            prev = digest
    return chain, prev


def _evidence_root_payload(record, chain):
    return {
        "plan_hash": record.get("plan_hash", ""),
        "tool_versions": record.get("tool_versions", {}),
        "skill_versions": record.get("skill_versions", {}),
        "scope_hash": record.get("scope_hash", ""),
        "actor": record.get("actor", ""),
        "approvals": record.get("approvals", []),
        "model_id": record.get("model_id", ""),
        "model_prompt_hash": record.get("model_prompt_hash", ""),
        "status": record.get("status", ""),
        "error": record.get("error"),
        "exit_code": record.get("exit_code"),
        "chain": chain,
    }


def _finalize_evidence(record):
    chain, root = _build_evidence_chain(record.get("steps", {}))
    record["evidence_chain"] = chain
    record["evidence_root"] = _hash_obj(_evidence_root_payload(record, chain))
    key = os.environ.get("SOS_EVIDENCE_KEY")
    if key:
        signature = hmac.new(key.encode("utf-8"), record["evidence_root"].encode("utf-8"),
                             hashlib.sha256).hexdigest()
        record["signature"] = {
            "algorithm": "hmac-sha256",
            "key_id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
            "value": signature,
        }
    return record


def _skill_versions(plan, app):
    versions = {}
    if app is None or not getattr(app, "orchestrator", None):
        return versions
    for step in plan.get("tools", []):
        if step.get("type") != "skill":
            continue
        skill = app.orchestrator.skills.get(step["name"])
        if not skill:
            continue
        module = sys.modules.get(skill.__class__.__module__)
        path = getattr(module, "__file__", None)
        if path:
            versions[step["name"]] = {"path": path, "sha256": _sha256(path)}
    return versions


def _execute_run(spec, run_id, base_record):
    from config.tools import ROUND_BASE_DIR
    from core.headless import HeadlessApp, HeadlessPanel
    from phases.phase2 import Phase2Freedom

    plan = spec["plan"]
    project_dir = Path(spec["project_dir"])
    task_name = spec["task_name"]
    round_num = int(spec["round"])
    timeout = float(spec.get("timeout", 0) or 0)
    registry = _registry(spec.get("tool_dirs", []))
    panel = HeadlessPanel(task_name, run_id, round_num=round_num, plan=plan)
    run_dir = _run_dir(run_id)
    cancel_path = run_dir / "cancel"
    cancel_requested = threading.Event()
    done = threading.Event()
    app = None
    phase2 = None
    error = None
    skill_versions_start = {}
    skill_versions_end = {}

    def watch_cancel():
        while not done.is_set():
            if cancel_path.exists():
                cancel_requested.set()
                if phase2 is not None:
                    try:
                        phase2.kill_task(run_id)
                    except Exception:
                        pass
                return
            done.wait(0.2)

    started_at = _now()
    t0 = time.time()
    record = dict(base_record)
    record.update({"status": "running", "started_at": started_at})

    try:
        if cancel_path.exists():
            cancel_requested.set()
        else:
            app = HeadlessApp(
                project_dir, spec["target"],
                tool_dirs=spec.get("tool_dirs", []),
                load_skills=not spec.get("no_skills", False),
            )
            skill_versions_start = _skill_versions(plan, app)
            phase2 = Phase2Freedom(app)
            watcher = threading.Thread(target=watch_cancel, daemon=True)
            watcher.start()
            phase2.run_task(run_id, panel, spec["target"])
            if run_id not in phase2.running_tasks:
                raise RuntimeError("任务没有启动：计划可能为空")
            info = phase2.running_tasks[run_id]
            deadline = (time.time() + timeout) if timeout > 0 else None
            timed_out = False
            while info.get("is_running"):
                if cancel_path.exists():
                    cancel_requested.set()
                    phase2.kill_task(run_id)
                    break
                if deadline is not None and time.time() > deadline:
                    timed_out = True
                    phase2.kill_task(run_id)
                    break
                time.sleep(0.1)
            thread = info.get("thread")
            if thread is not None:
                thread.join(timeout=5)
            if timed_out:
                error = f"执行超时（{timeout:g}s）"
    except CLIError:
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        done.set()
        if app is not None:
            skill_versions_end = _skill_versions(plan, app)
            try:
                app.close()
            except Exception:
                pass

    summary_path = ROUND_BASE_DIR / task_name / f"{task_name}_round_{round_num}.json"
    summary = {}
    if summary_path.exists():
        try:
            summary = _read_json(summary_path)
        except CLIError:
            summary = {}
    steps = summary.get("tools", {}) if isinstance(summary, dict) else {}
    flat = [r for results in steps.values() if isinstance(results, list) for r in results if isinstance(r, dict)]
    all_ok = bool(flat) and all(bool(r.get("success")) for r in flat)
    tool_versions_end = _tool_versions(plan, registry)
    tool_frozen = base_record.get("tool_versions", {}) == tool_versions_end
    skill_frozen = skill_versions_start == skill_versions_end
    if (not tool_frozen or not skill_frozen) and not error:
        error = "工具/skill 在运行期间发生变化（版本冻结校验失败）"
    if cancel_requested.is_set():
        status = "cancelled"
    elif error:
        status = "failed"
    elif all_ok:
        status = "success"
    else:
        status = "failed"

    record.update({
        "status": status,
        "success": status == "success",
        "finished_at": _now(),
        "duration": round(time.time() - t0, 3),
        "exit_code": 0 if status == "success" else 3,
        "steps": steps,
        "summary_path": str(summary_path),
        "session_log": str(project_dir / "session.log"),
        "structured_logs": str(project_dir / "structured_logs"),
        "tool_versions_start": base_record.get("tool_versions", {}),
        "tool_versions": tool_versions_end,
        "tool_frozen": tool_frozen,
        "skill_versions_start": skill_versions_start,
        "skill_versions": skill_versions_end,
        "skill_frozen": skill_frozen,
        "log_tail": panel.logs[-200:],
        "cancel_requested": cancel_requested.is_set(),
    })
    if error:
        record["error"] = error
    _finalize_evidence(record)
    _write_json(run_dir / "run.json", record)
    return record


def _kill_process_tree(pid):
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass


def _spawn_worker(run_id):
    from config.tools import DATA_DIR, RUNS_DIR

    run_dir = RUNS_DIR / run_id
    worker_log = run_dir / "worker.log"
    log = open(worker_log, "ab")
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--data-dir", str(DATA_DIR),
        "_worker", "--run-id", run_id,
    ]
    env = os.environ.copy()
    env["SOS_DATA_DIR"] = str(DATA_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True, env=env, **kwargs,
        )
    finally:
        log.close()
    return proc, worker_log


def cmd_run(args):
    from config.tools import ensure_data_dirs

    ensure_data_dirs()
    registry, plan, project_dir, task_name, run_id, spec = _prepare_run(args)
    base = _base_record(spec, run_id)
    run_dir = _run_dir(run_id)
    _write_json(run_dir / "run.json", base)

    if getattr(args, "detach", False):
        try:
            proc, worker_log = _spawn_worker(run_id)
        except Exception as e:
            base.update({"status": "failed", "error": f"worker 启动失败: {e}",
                         "finished_at": _now(), "exit_code": 3})
            _finalize_evidence(base)
            _write_json(run_dir / "run.json", base)
            raise CLIError("worker_failed", f"worker 启动失败: {e}", 3)
        base.update({
            "status": "running",
            "pid": proc.pid,
            "worker_log": str(worker_log),
            "started_at": _now(),
        })
        _write_json(run_dir / "run.json", base)
        base["run_path"] = str(run_dir / "run.json")
        if args.json:
            _print_json(base)
        else:
            print(f"run_id : {run_id}")
            print(f"status : running (detached pid={proc.pid})")
            print(f"run    : {run_dir / 'run.json'}")
        return 0

    base.update({"status": "running", "pid": os.getpid()})
    _write_json(run_dir / "run.json", base)
    try:
        record = _execute_run(spec, run_id, base)
    except Exception as e:
        record = dict(base)
        record.update({"status": "failed", "error": f"{type(e).__name__}: {e}",
                       "finished_at": _now(), "exit_code": 3})
        _finalize_evidence(record)
        _write_json(run_dir / "run.json", record)
    record["run_path"] = str(run_dir / "run.json")
    if args.json:
        _print_json(record)
    else:
        print(f"run_id : {run_id}")
        print(f"status : {record.get('status')}")
        print(f"task   : {task_name}  round={spec['round']}")
        print(f"summary: {record.get('summary_path')}")
        if record.get("error"):
            print(f"error  : {record['error']}", file=sys.stderr)
    return 0 if record.get("status") == "success" else 3


def cmd_worker(args):
    from config.tools import ensure_data_dirs

    ensure_data_dirs()
    run_id = _safe_run_id(args.run_id)
    run_dir = _run_dir(run_id)
    spec_path = run_dir / "spec.json"
    if not spec_path.exists():
        raise CLIError("not_found", f"缺少 spec: {spec_path}")
    spec = _read_json(spec_path)
    base = _base_record(spec, run_id)
    base.update({"status": "running", "pid": os.getpid(), "started_at": _now()})
    _write_json(run_dir / "run.json", base)
    try:
        record = _execute_run(spec, run_id, base)
    except Exception as e:
        record = dict(base)
        record.update({"status": "failed", "error": f"{type(e).__name__}: {e}",
                       "finished_at": _now(), "exit_code": 3})
        _finalize_evidence(record)
        _write_json(run_dir / "run.json", record)
        return 3
    return 0 if record.get("status") == "success" else 3


def cmd_runs_cancel(args):
    run_id = _safe_run_id(args.run_id)
    run_dir = _run_dir(run_id)
    record = _read_json(run_dir / "run.json")
    if record.get("status") in ("success", "failed", "cancelled"):
        if args.json:
            _print_json({"ok": True, "run_id": run_id, "status": record["status"],
                         "message": "run already finished"})
        else:
            print(f"run {run_id} already finished: {record['status']}")
        return 0

    record["cancel_requested"] = True
    record["cancel_requested_at"] = _now()
    record["cancel_requested_by"] = args.by
    _write_json(run_dir / "run.json", record)
    (run_dir / "cancel").write_text(_now(), encoding="utf-8")

    deadline = time.time() + max(1, int(args.wait))
    final = record
    while time.time() < deadline:
        try:
            final = _read_json(run_dir / "run.json")
        except CLIError:
            final = record
        if final.get("status") in ("success", "failed", "cancelled"):
            break
        time.sleep(0.2)

    if final.get("status") not in ("success", "failed", "cancelled"):
        if final.get("pid"):
            _kill_process_tree(final["pid"])
        final["status"] = "cancelled"
        final["success"] = False
        final["exit_code"] = 3
        final["finished_at"] = _now()
        final["error"] = "cancel requested; worker did not acknowledge, process tree killed"
        _finalize_evidence(final)
        _write_json(run_dir / "run.json", final)

    if args.json:
        _print_json({"ok": True, "run_id": run_id, "status": final.get("status")})
    else:
        print(f"run_id : {run_id}")
        print(f"status : {final.get('status')}")
    return 0


def cmd_runs_list(args):
    from config.tools import RUNS_DIR, ensure_data_dirs

    ensure_data_dirs()
    items = []
    for path in sorted(RUNS_DIR.glob("*/run.json"), reverse=True):
        try:
            data = _read_json(path)
        except CLIError:
            continue
        items.append({
            "run_id": data.get("run_id", path.parent.name),
            "status": data.get("status", "unknown"),
            "task_name": data.get("task_name", ""),
            "round": data.get("round", 1),
            "target": data.get("target", ""),
            "started_at": data.get("started_at", ""),
        })
    if args.json:
        _print_json({"ok": True, "schema": "sos.runs/v1", "runs": items})
    else:
        for item in items:
            print(f"{item['run_id']}\t{item['status']}\t{item['task_name']}\t{item['target']}")
    return 0


def cmd_runs_show(args):
    _, record = _load_run(args.run_id)
    if args.json:
        _print_json({"ok": True, "run": record})
    else:
        for key in ("run_id", "status", "task_name", "round", "target", "started_at", "finished_at", "summary_path"):
            print(f"{key}: {record.get(key)}")
        if record.get("error"):
            print(f"error: {record['error']}")
    return 0


def cmd_runs_logs(args):
    _, record = _load_run(args.run_id)
    path = Path(record.get("session_log", ""))
    tail = int(args.tail or 0)
    follow = bool(getattr(args, "follow", False))

    if not follow:
        if not path.is_file():
            raise CLIError("not_found", f"session.log 不存在: {path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if tail > 0:
            lines = lines[-tail:]
        if args.json:
            _print_json({
                "ok": True,
                "schema": "sos.run_logs/v1",
                "run_id": record["run_id"],
                "path": str(path),
                "lines": lines,
            })
        else:
            for line in lines:
                print(line)
        return 0

    # --follow：先输出已有内容，再轮询新行，直到 run 进入终态
    offset = 0
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        initial = lines[-tail:] if tail > 0 else lines
        for line in initial:
            print(json.dumps({"run_id": record["run_id"], "line": line}, ensure_ascii=False)
                  if args.json else line, flush=True)
        offset = len(lines)

    while True:
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) < offset:
                offset = 0
            for line in lines[offset:]:
                print(json.dumps({"run_id": record["run_id"], "line": line}, ensure_ascii=False)
                      if args.json else line, flush=True)
            offset = len(lines)
        try:
            _, current = _load_run(args.run_id)
        except CLIError:
            break
        if current.get("status") in ("success", "failed", "cancelled"):
            # 最后再读一次，避免终态前最后几行丢失
            if path.is_file():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[offset:]:
                    print(json.dumps({"run_id": record["run_id"], "line": line}, ensure_ascii=False)
                          if args.json else line, flush=True)
            break
        time.sleep(0.2)
    return 0


def cmd_runs_verify(args):
    run_id = _safe_run_id(args.run_id)
    _, record = _load_run(run_id)
    chain, root = _build_evidence_chain(record.get("steps", {}))
    expected_root = _hash_obj(_evidence_root_payload(record, chain))
    chain_ok = chain == record.get("evidence_chain", [])
    root_ok = expected_root == record.get("evidence_root", "")
    signature_ok = None
    key = os.environ.get("SOS_EVIDENCE_KEY")
    if key:
        sig = record.get("signature") or {}
        expected = hmac.new(key.encode("utf-8"), expected_root.encode("utf-8"),
                            hashlib.sha256).hexdigest()
        signature_ok = bool(sig) and hmac.compare_digest(expected, sig.get("value", ""))
    result = {
        "ok": bool(chain_ok and root_ok and (signature_ok is not False)),
        "schema": "sos.run_verify/v1",
        "run_id": run_id,
        "chain_entries": len(chain),
        "chain_ok": chain_ok,
        "root_ok": root_ok,
        "signature_ok": signature_ok,
        "evidence_root": expected_root,
    }
    if args.json:
        _print_json(result)
    else:
        print(f"run_id: {run_id}")
        print(f"chain : {'ok' if chain_ok else 'FAILED'} ({len(chain)} entries)")
        print(f"root  : {'ok' if root_ok else 'FAILED'}")
        if signature_ok is not None:
            print(f"sig   : {'ok' if signature_ok else 'FAILED'}")
    return 0 if result["ok"] else 3


def cmd_runs_bundle(args):
    from config.tools import ensure_data_dirs

    ensure_data_dirs()
    _, record = _load_run(args.run_id)
    summary = {}
    summary_path = Path(record.get("summary_path", ""))
    if summary_path.is_file():
        try:
            summary = _read_json(summary_path)
        except CLIError:
            summary = {}

    artifacts = []

    def add(path: Path):
        path = Path(path)
        if path.is_file():
            try:
                rel = str(path.relative_to(Path(record["project_dir"])))
            except ValueError:
                rel = str(path)
            artifacts.append({
                "path": str(path),
                "relative": rel,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            })

    add(summary_path)
    add(Path(record.get("session_log", "")))
    from core import targets as target_api

    latest_round = (
        Path(record["project_dir"]) / "latest"
        / target_api.slug(record.get("target", ""))
        / f"round_{record.get('round', 1)}"
    )
    if latest_round.is_dir():
        for path in sorted(latest_round.rglob("*.json")):
            add(path)
    structured_dir = Path(record.get("structured_logs", ""))
    if structured_dir.is_dir():
        for path in sorted(structured_dir.rglob("*.jsonl")):
            add(path)

    bundle = {
        "schema": "sos.run_bundle/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run": record,
        "round_summary": summary,
        "artifacts": artifacts,
    }
    if args.out:
        out = Path(args.out).expanduser().resolve()
        _write_json(out, bundle)
        result = {
            "ok": True,
            "schema": "sos.run_bundle_result/v1",
            "run_id": record["run_id"],
            "bundle_path": str(out),
            "artifacts": len(artifacts),
        }
        if args.json:
            _print_json(result)
        else:
            print(f"bundle: {out}")
            print(f"artifacts: {len(artifacts)}")
    else:
        _print_json(bundle)
    return 0


def _static_checks(path):
    """静态检查：编译 + 对常见危险调用给出 finding（不直接执行代码）。"""
    path = Path(path)
    findings = []
    compiled = None
    if path.suffix.lower() == ".py":
        try:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
            compile(source, str(path), "exec")
            tree = ast.parse(source, filename=str(path))
            compiled = True
        except (OSError, SyntaxError, ValueError) as e:
            raise CLIError("artifact_invalid", f"Python 编译失败: {e}")

        def visit(node):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec", "compile"):
                    findings.append(f"line {getattr(node, 'lineno', '?')}: call {node.func.id}()")
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("system", "popen"):
                    findings.append(f"line {getattr(node, 'lineno', '?')}: call .{node.func.attr}()")
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        findings.append(f"line {getattr(node, 'lineno', '?')}: shell=True")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(n == "ctypes" or n.startswith("ctypes.") for n in names):
                    findings.append(f"line {getattr(node, 'lineno', '?')}: import ctypes")

        for node in ast.walk(tree):
            visit(node)
    return {"compiled": compiled, "findings": findings}


def _staging_manifest(kind, name):
    from config.tools import STAGING_DIR

    path = STAGING_DIR / kind / _safe_task_name(name) / "manifest.json"
    if not path.exists():
        raise CLIError("not_found", f"没有 staged {kind}: {name}")
    return path, _read_json(path)


def _cmd_stage(kind, args):
    from config.tools import STAGING_DIR, TOOL_EXTENSIONS

    source = Path(args.path).expanduser().resolve()
    if not source.is_file():
        raise CLIError("not_found", f"文件不存在: {source}")
    name = _safe_task_name(args.name or source.stem)
    if kind == "tool" and source.suffix.lower() not in TOOL_EXTENSIONS:
        raise CLIError("artifact_invalid", f"不支持的工具后缀: {source.suffix}")
    if kind == "skill" and source.suffix.lower() != ".py":
        raise CLIError("artifact_invalid", "skill 必须是 .py")

    dest_dir = STAGING_DIR / kind / name
    if dest_dir.exists():
        if not args.force:
            raise CLIError("already_exists", f"staging 已存在: {dest_dir}（使用 --force 覆盖）")
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = dest_dir / f"{name}{source.suffix.lower()}"
    shutil.copy2(source, copied)
    checks = _static_checks(copied)
    manifest = {
        "kind": kind,
        "name": name,
        "filename": copied.name,
        "version": str(args.version or "0.1.0"),
        "risk": args.risk,
        "description": getattr(args, "description", "") or "",
        "category": getattr(args, "category", "") or "",
        "source": str(source),
        "sha256": _sha256(copied),
        "staged_at": _now(),
        "status": "staged",
        "checks": checks,
        "dry_run_confirmed": False,
    }
    _write_json(dest_dir / "manifest.json", manifest)
    result = {
        "ok": True,
        "schema": "sos.stage/v1",
        "kind": kind,
        "name": name,
        "staging_path": str(dest_dir),
        "sha256": manifest["sha256"],
        "checks": checks,
    }
    if args.json:
        _print_json(result)
    else:
        print(f"staged {kind}: {name}")
        print(f"path  : {dest_dir}")
        print(f"checks: {len(checks['findings'])} finding(s)")
    return 0


def cmd_tools_stage(args):
    return _cmd_stage("tool", args)


def cmd_skills_stage(args):
    return _cmd_stage("skill", args)


def _cmd_stage_list(kind, args):
    from config.tools import STAGING_DIR

    items = []
    base = STAGING_DIR / kind
    if base.is_dir():
        for manifest_path in sorted(base.glob("*/manifest.json")):
            try:
                data = _read_json(manifest_path)
            except CLIError:
                continue
            items.append({
                "name": data.get("name"),
                "version": data.get("version"),
                "risk": data.get("risk"),
                "status": data.get("status"),
                "sha256": data.get("sha256"),
                "staged_at": data.get("staged_at"),
                "path": str(manifest_path.parent),
            })
    if args.json:
        _print_json({"ok": True, "schema": f"sos.staged_{kind}/v1", "items": items})
    else:
        for item in items:
            print(f"{item['name']}\t{item['version']}\t{item['risk']}\t{item['status']}")
    return 0


def cmd_tools_staged(args):
    return _cmd_stage_list("tool", args)


def cmd_skills_staged(args):
    return _cmd_stage_list("skill", args)


def _cmd_stage_approve(kind, args):
    from config.tools import MANAGED_SKILLS_DIR, MANAGED_TOOLS_DIR

    manifest_path, manifest = _staging_manifest(kind, args.name)
    if not args.by.strip():
        raise CLIError("approval_required", "--by 必须填写审批人")
    if not args.dry_run_confirmed:
        raise CLIError("approval_required", "必须先用只读目标 dry-run，并传 --dry-run-confirmed")
    if args.strict and manifest.get("checks", {}).get("findings"):
        raise CLIError("checks_failed", "静态检查有 finding；请修复后重新 stage，或去掉 --strict")

    source = manifest_path.parent / manifest["filename"]
    if not source.is_file():
        raise CLIError("not_found", f"staged 文件丢失: {source}")

    if kind == "tool":
        dest_dir = MANAGED_TOOLS_DIR
    else:
        category = str(manifest.get("category") or "phase3")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", category):
            raise CLIError("artifact_invalid", f"非法 skill category: {category}")
        dest_dir = MANAGED_SKILLS_DIR / category
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if dest.exists() and not args.force:
        raise CLIError("already_exists", f"目标已存在: {dest}（使用 --force 覆盖）")
    shutil.copy2(source, dest)

    approved = {
        "kind": kind,
        "name": manifest["name"],
        "version": manifest.get("version", "0.1.0"),
        "risk": manifest.get("risk", "unknown"),
        "description": manifest.get("description", ""),
        "sha256": _sha256(dest),
        "source": manifest.get("source"),
        "staged_at": manifest.get("staged_at"),
        "approved_by": args.by,
        "approved_at": _now(),
        "approval_note": args.note or "",
        "checks": manifest.get("checks", {}),
        "dry_run_confirmed": True,
        "path": str(dest),
    }
    _write_json(dest.with_suffix(".json"), approved)
    manifest.update({
        "status": "approved",
        "approved_by": args.by,
        "approved_at": approved["approved_at"],
        "dry_run_confirmed": True,
        "installed_path": str(dest),
    })
    _write_json(manifest_path, manifest)

    result = {
        "ok": True,
        "schema": "sos.approve/v1",
        "kind": kind,
        "name": manifest["name"],
        "installed_path": str(dest),
        "sha256": approved["sha256"],
        "approved_by": args.by,
    }
    if args.json:
        _print_json(result)
    else:
        print(f"approved {kind}: {manifest['name']}")
        print(f"installed: {dest}")
    return 0


def cmd_tools_approve(args):
    return _cmd_stage_approve("tool", args)


def cmd_skills_approve(args):
    return _cmd_stage_approve("skill", args)


def _cmd_stage_reject(kind, args):
    from config.tools import STAGING_DIR

    manifest_path, manifest = _staging_manifest(kind, args.name)
    if not args.by.strip():
        raise CLIError("approval_required", "--by 必须填写审批人")
    manifest.update({"status": "rejected", "rejected_by": args.by, "rejected_at": _now(),
                     "rejection_note": args.note or ""})
    _write_json(manifest_path, manifest)
    result = {"ok": True, "schema": "sos.reject/v1", "kind": kind,
              "name": manifest["name"], "status": "rejected"}
    if args.json:
        _print_json(result)
    else:
        print(f"rejected {kind}: {manifest['name']}")
    return 0


def cmd_tools_reject(args):
    return _cmd_stage_reject("tool", args)


def cmd_skills_reject(args):
    return _cmd_stage_reject("skill", args)


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="sos",
        description="SOS_Workbench CLI：DSH 下游的计划执行与执行证据入口。",
    )
    parser.add_argument("--version", action="version", version=f"sos {VERSION}")
    parser.add_argument("--data-dir", default=argparse.SUPPRESS, help="运行数据目录（默认 storage/）")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="只输出 JSON")
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p_tools = sub.add_parser("tools", parents=[common], help="工具发现、校验与入库")
    tools_sub = p_tools.add_subparsers(dest="tools_command", required=True)
    pt_list = tools_sub.add_parser("list", parents=[common], help="列出已发现工具")
    pt_list.add_argument("--tool-dir", action="append", default=[], help="额外工具目录，可重复")
    pt_list.set_defaults(func=cmd_tools_list)
    pt_val = tools_sub.add_parser("validate", parents=[common], help="校验一个工具文件")
    pt_val.add_argument("--path", required=True)
    pt_val.set_defaults(func=cmd_tools_validate)
    pt_stage = tools_sub.add_parser("stage", parents=[common], help="把工具放入 staging")
    pt_stage.add_argument("--path", required=True)
    pt_stage.add_argument("--name")
    pt_stage.add_argument("--version", default="0.1.0")
    pt_stage.add_argument("--risk", choices=["read_only", "destructive", "unknown"], default="unknown")
    pt_stage.add_argument("--description", default="")
    pt_stage.add_argument("--force", action="store_true")
    pt_stage.set_defaults(func=cmd_tools_stage)
    pt_approve = tools_sub.add_parser("approve", parents=[common], help="人工批准 staged 工具")
    pt_approve.add_argument("--name", required=True)
    pt_approve.add_argument("--by", required=True)
    pt_approve.add_argument("--note", default="")
    pt_approve.add_argument("--dry-run-confirmed", action="store_true")
    pt_approve.add_argument("--strict", action="store_true")
    pt_approve.add_argument("--force", action="store_true")
    pt_approve.set_defaults(func=cmd_tools_approve)
    pt_staged = tools_sub.add_parser("staged", parents=[common], help="列出 staged 工具")
    pt_staged.set_defaults(func=cmd_tools_staged)
    pt_reject = tools_sub.add_parser("reject", parents=[common], help="拒绝 staged 工具")
    pt_reject.add_argument("--name", required=True)
    pt_reject.add_argument("--by", required=True)
    pt_reject.add_argument("--note", default="")
    pt_reject.set_defaults(func=cmd_tools_reject)

    p_skills = sub.add_parser("skills", parents=[common], help="技能发现、校验与入库")
    skills_sub = p_skills.add_subparsers(dest="skills_command", required=True)
    ps_stage = skills_sub.add_parser("stage", parents=[common], help="把技能放入 staging")
    ps_stage.add_argument("--path", required=True)
    ps_stage.add_argument("--name")
    ps_stage.add_argument("--category", default="phase3")
    ps_stage.add_argument("--version", default="0.1.0")
    ps_stage.add_argument("--risk", choices=["read_only", "destructive", "unknown"], default="unknown")
    ps_stage.add_argument("--description", default="")
    ps_stage.add_argument("--force", action="store_true")
    ps_stage.set_defaults(func=cmd_skills_stage)
    ps_approve = skills_sub.add_parser("approve", parents=[common], help="人工批准 staged 技能")
    ps_approve.add_argument("--name", required=True)
    ps_approve.add_argument("--by", required=True)
    ps_approve.add_argument("--note", default="")
    ps_approve.add_argument("--dry-run-confirmed", action="store_true")
    ps_approve.add_argument("--strict", action="store_true")
    ps_approve.add_argument("--force", action="store_true")
    ps_approve.set_defaults(func=cmd_skills_approve)
    ps_staged = skills_sub.add_parser("staged", parents=[common], help="列出 staged 技能")
    ps_staged.set_defaults(func=cmd_skills_staged)
    ps_reject = skills_sub.add_parser("reject", parents=[common], help="拒绝 staged 技能")
    ps_reject.add_argument("--name", required=True)
    ps_reject.add_argument("--by", required=True)
    ps_reject.add_argument("--note", default="")
    ps_reject.set_defaults(func=cmd_skills_reject)

    p_plan = sub.add_parser("plan", parents=[common], help="计划校验与提交")
    plan_sub = p_plan.add_subparsers(dest="plan_command", required=True)
    pp_val = plan_sub.add_parser("validate", parents=[common], help="校验计划 JSON")
    pp_val.add_argument("--plan", required=True)
    pp_val.add_argument("--tool-dir", action="append", default=[])
    pp_val.set_defaults(func=cmd_plan_validate)
    pp_sub = plan_sub.add_parser("submit", parents=[common], help="提交计划到 storage/round")
    pp_sub.add_argument("--plan", required=True)
    pp_sub.add_argument("--task-name")
    pp_sub.add_argument("--tool-dir", action="append", default=[])
    pp_sub.set_defaults(func=cmd_plan_submit)

    p_run = sub.add_parser("run", parents=[common], help="同步执行一条计划")
    p_run.add_argument("--plan", required=True)
    p_run.add_argument("--project", default="cli", help="项目名（默认 cli）")
    p_run.add_argument("--project-dir", help="直接指定项目目录（优先于 --project）")
    p_run.add_argument("--target", required=True)
    p_run.add_argument("--task-name")
    p_run.add_argument("--round", type=int, default=1)
    p_run.add_argument("--timeout", type=float, default=0, help="整次 run 的超时秒数；0 表示不限")
    p_run.add_argument("--tool-dir", action="append", default=[])
    p_run.add_argument("--no-skills", action="store_true", help="不加载 skills/（只跑工具步骤）")
    p_run.add_argument("--detach", action="store_true", help="后台 worker 执行，立即返回 run_id")
    p_run.add_argument("--scope", help="scope/allowlist JSON；限制 target/tools/budget")
    p_run.add_argument("--actor", choices=["human", "dsh", "sos-reviewer"], default="human")
    p_run.add_argument("--approved-by", default="", help="审批人；破坏性动作必填")
    p_run.add_argument("--approval-note", default="")
    p_run.add_argument("--allow-destructive", action="store_true")
    p_run.add_argument("--require-manifest", action="store_true", help="工具必须有 manifest/risk")
    p_run.add_argument("--max-steps", type=int, default=0)
    p_run.add_argument("--max-duration", type=float, default=0)
    p_run.add_argument("--parent-run-id", default=None)
    p_run.add_argument("--plan-version", default="1")
    p_run.add_argument("--model-id", default="")
    p_run.add_argument("--model-prompt-hash", default="")
    p_run.set_defaults(func=cmd_run)

    p_runs = sub.add_parser("runs", parents=[common], help="历史 run 查询与证据导出")
    runs_sub = p_runs.add_subparsers(dest="runs_command", required=True)
    pr_list = runs_sub.add_parser("list", parents=[common], help="列出 run")
    pr_list.set_defaults(func=cmd_runs_list)
    pr_show = runs_sub.add_parser("show", parents=[common], help="查看 run 记录")
    pr_show.add_argument("--run-id", required=True)
    pr_show.set_defaults(func=cmd_runs_show)
    pr_logs = runs_sub.add_parser("logs", parents=[common], help="查看 session.log")
    pr_logs.add_argument("--run-id", required=True)
    pr_logs.add_argument("--tail", type=int, default=100, help="只显示最后 N 行；0 表示全部")
    pr_logs.add_argument("--follow", action="store_true", help="持续跟随，直到 run 终态")
    pr_logs.set_defaults(func=cmd_runs_logs)
    pr_cancel = runs_sub.add_parser("cancel", parents=[common], help="请求取消一个 run")
    pr_cancel.add_argument("--run-id", required=True)
    pr_cancel.add_argument("--by", default="human")
    pr_cancel.add_argument("--wait", type=int, default=10)
    pr_cancel.set_defaults(func=cmd_runs_cancel)
    pr_verify = runs_sub.add_parser("verify", parents=[common], help="校验证据链与签名")
    pr_verify.add_argument("--run-id", required=True)
    pr_verify.set_defaults(func=cmd_runs_verify)
    pr_bundle = runs_sub.add_parser("bundle", parents=[common], help="导出一轮的执行证据包")
    pr_bundle.add_argument("--run-id", required=True)
    pr_bundle.add_argument("--out", help="输出文件；省略则打印到 stdout")
    pr_bundle.set_defaults(func=cmd_runs_bundle)

    # 内部 worker，不出现在 --help 中
    p_worker = sub.add_parser("_worker", parents=[common])
    p_worker.add_argument("--run-id", required=True)
    p_worker.set_defaults(func=cmd_worker)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _bootstrap_data_dir(argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CLIError as e:
        if getattr(args, "json", False):
            _print_json({"ok": False, "schema": "sos.error/v1", "code": e.code, "message": e.message})
        else:
            print(f"error: {e.message}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as e:
        if getattr(args, "json", False):
            _print_json({"ok": False, "schema": "sos.error/v1", "code": "internal", "message": str(e)})
        else:
            print(f"internal error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
