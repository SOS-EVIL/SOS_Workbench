"""
test/inheritance.py - 步骤参数继承（inherit_from）回归测试

验证：
  1. 第一步结果会写回 task_data
  2. 第二步通过 inherit_from 收到上一步的结果对象
  3. 继承对象里保留第一步的真实输出
  4. 没有 inherit_from 的步骤行为不变

运行： python test/inheritance.py
"""

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import phases.phase2 as phase2                      # noqa: E402
from core.task_executor import TaskExecutor          # noqa: E402
from core.tool_registry import ToolRegistry          # noqa: E402
from phases.phase1 import Phase1Collector            # noqa: E402

RESULTS = []

def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   << " + str(detail)) if (detail and not ok) else ""))

class StubRoot:
    def after(self, delay, fn, *a, **kw):
        fn()

class StubStructuredLogger:
    def __getattr__(self, name):
        return lambda *a, **kw: None

class StubPanel:
    def __init__(self):
        self.logs = []
        self.current_round = 1
        self.task_name = "inherit_demo"
        self.task_id = "inherit_demo_1"

    def append_log(self, msg):
        self.logs.append(str(msg))

    def advance_round(self):
        self.current_round += 1

class StubApp:
    def __init__(self, project_dir, registry, executor):
        self.project_dir = project_dir
        self.tool_registry = registry
        self.executor = executor
        self.structured_logger = StubStructuredLogger()
        self.root = StubRoot()
        self.phase1 = Phase1Collector(self, executor=executor)
        self.freedom_panels = {}
        self.phase3_panels = {}
        self.orchestrator = SimpleNamespace(skills={})

    def log_global(self, msg, host=None, sensitive=False):
        pass

    def log_target(self, host, msg):
        pass

    def log_target_program(self, host, msg):
        pass

    def store_tool_output(self, host, tool_name, lines):
        pass

    def register_freedom_thread(self, host, pause_event):
        pass

    def unregister_freedom_thread(self, host):
        pass

ECHO_TOOL = '''
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--target", default="")
p.add_argument("--config", default="{}")
a = p.parse_args()
try:
    cfg = json.loads(a.config or "{}")
except json.JSONDecodeError:
    cfg = {}
print("CONFIG=" + json.dumps(cfg, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
print("__JSON_RESULT_START__")
print(json.dumps({"success": True, "echo": cfg}, ensure_ascii=False))
print("__JSON_RESULT_END__")
'''

def configs_from(panel):
    out = []
    for line in panel.logs:
        marker = "CONFIG="
        if marker in line:
            try:
                out.append(json.loads(line.split(marker, 1)[1]))
            except json.JSONDecodeError:
                pass
    return out

def prep(freedom, panel, host):
    freedom.running_tasks[panel.task_id] = {
        "is_running": True, "thread": None, "panel": panel, "host": host,
        "pause_event": threading.Event(), "current_round": panel.current_round,
        "task_name": panel.task_name,
    }
    freedom.task_data[panel.task_id] = {}

def main():
    tmp = Path(tempfile.mkdtemp(prefix="sos_inherit_"))
    tools = tmp / "tools"
    tools.mkdir()
    (tools / "echo_config.py").write_text(ECHO_TOOL, encoding="utf-8")
    project = tmp / "project"
    project.mkdir()
    phase2.ROUND_BASE_DIR = tmp / "round"

    try:
        registry = ToolRegistry(tool_dirs=[tools])
        executor = TaskExecutor(concurrency_getter=lambda: 2,
                                tool_resolver=lambda: registry.get_all_tools(),
                                default_timeout=30)
        app = StubApp(project, registry, executor)
        freedom = phase2.Phase2Freedom(app)
        panel = StubPanel()
        prep(freedom, panel, "target-1")

        chain = [
            {"name": "echo_config", "type": "tool",
             "params": json.dumps({"value": "hello"}), "inherit_from": None, "inherit_file": None},
            {"name": "echo_config", "type": "tool",
             "params": "{}", "inherit_from": "echo_config", "inherit_file": None},
        ]
        freedom._execute_tool_chain(panel.task_id, panel, "target-1", chain,
                                   threading.Event(), 1, panel.task_name)

        configs = configs_from(panel)
        check("两步都执行了", len(configs) == 2, configs)
        if len(configs) == 2:
            first, second = configs
            inherited = second.get("inherit_data")
            check("第二步收到继承对象", isinstance(inherited, dict), second)
            check("继承对象保留第一步结果",
                  isinstance(inherited, dict)
                  and inherited.get("result", {}).get("echo") == {"value": "hello"},
                  inherited)

        # 没有 inherit_from 的普通步骤不应被注入 inherit_data
        panel2 = StubPanel()
        panel2.task_id = "inherit_demo_2"
        prep(freedom, panel2, "target-2")
        chain2 = [{"name": "echo_config", "type": "tool",
                   "params": json.dumps({"value": "solo"}), "inherit_from": None, "inherit_file": None}]
        freedom._execute_tool_chain(panel2.task_id, panel2, "target-2", chain2,
                                   threading.Event(), 1, panel2.task_name)
        configs2 = configs_from(panel2)
        check("无 inherit_from 的步骤不被注入", len(configs2) == 1 and "inherit_data" not in configs2[0], configs2)
        check("task_data 已写回",
              freedom.task_data.get(panel2.task_id, {}).get("echo_config", {})
              .get("result", {}).get("echo") == {"value": "solo"},
              freedom.task_data.get(panel2.task_id))
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
