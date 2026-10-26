"""
test/integration_phase2.py - 无 GUI 的编排链路集成测试

直接驱动 phases/phase2.py 的 _execute_tool_chain，验证：
  1. 非网页 target（sqlite 文件）能作为二级任务的一步被真正执行
  2. params 被真实下传（不再静默丢弃）
  3. round_N.json / latest/{slug}/round_N/ 两类产物都按老格式落盘
  4. 阶段 3（只读消费方）的 get_round_summary_text 仍能读到结果
  5. 凭证缺失时该步失败但链路不崩

运行： python test/integration_phase2.py
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import phases.phase2 as phase2                      # noqa: E402
import phases.phase3 as phase3                      # noqa: E402
from core.tool_registry import ToolRegistry         # noqa: E402
from core.task_executor import TaskExecutor         # noqa: E402
from phases.phase1 import Phase1Collector           # noqa: E402
from skills.phase3.chat_bot import ChatBotSkill     # noqa: E402

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
        self.task_name = "demo_task"
        self.task_id = "demo_task_1"

    def append_log(self, msg):
        self.logs.append(str(msg))

    def advance_round(self):
        self.current_round += 1

    def get_tool_boxes_data(self):
        return {"tools": []}

class StubApp:
    def __init__(self, project_dir, executor, registry):
        self.project_dir = project_dir
        self.executor = executor
        self.tool_registry = registry
        self.structured_logger = StubStructuredLogger()
        self.root = StubRoot()
        self.global_logs = []
        self.target_logs = []
        self.program_logs = []
        self.phase1 = Phase1Collector(self, executor=executor)
        self.freedom_panels = {}
        self.phase3_panels = {}
        self.orchestrator = SimpleNamespace(skills={})

    def log_global(self, msg, host=None, sensitive=False):
        self.global_logs.append(msg)

    def log_target(self, host, msg):
        self.target_logs.append((host, msg))

    def log_target_program(self, host, msg):
        self.program_logs.append((host, msg))

    def store_tool_output(self, host, tool_name, lines):
        pass

    def register_freedom_thread(self, host, pause_event):
        pass

    def unregister_freedom_thread(self, host):
        pass

def prep(freedom, panel, host):
    """模拟 run_task() 里的任务注册（_execute_tool_chain 直接读 running_tasks）。"""
    freedom.running_tasks[panel.task_id] = {
        "is_running": True, "thread": None, "panel": panel, "host": host,
        "pause_event": threading.Event(), "current_round": panel.current_round,
        "task_name": panel.task_name,
    }
    freedom.task_data[panel.task_id] = {}

def build_chain(sql, inherit_from=None, inherit_file=None):
    return [{
        "name": "sqlite_query",
        "type": "tool",
        "params": json.dumps({"sql": sql, "limit": 5}, ensure_ascii=False),
        "inherit_from": inherit_from,
        "inherit_file": inherit_file,
    }]

def main():
    tmp = Path(tempfile.mkdtemp(prefix="sos_int_"))
    round_dir = tmp / "round_data"
    project = tmp / "project"
    project.mkdir()
    round_dir.mkdir()

    # 让 phase2 / phase3 都写到临时目录，避免污染 storage/
    phase2.ROUND_BASE_DIR = round_dir
    phase3.ROUND_BASE_DIR = round_dir

    try:
        # 构造一个 SQLite 数据库作为"非网页 target"
        db = project / "analytics.db"
        conn = sqlite3.connect(db)
        conn.execute("create table events(id integer, kind text)")
        conn.executemany("insert into events values(?,?)",
                         [(1, "login"), (2, "purchase"), (3, "login")])
        conn.commit()
        conn.close()

        registry = ToolRegistry()
        executor = TaskExecutor(concurrency_getter=lambda: 3,
                               tool_resolver=lambda: registry.get_all_tools(),
                               default_timeout=60)
        app = StubApp(project, executor, registry)
        freedom = phase2.Phase2Freedom(app)
        panel = StubPanel()

        # 场景 1：正常执行
        print("\n[1] 非网页 target 走二级编排")
        chain = build_chain("select kind, count(*) as n from events group by kind order by n desc")
        prep(freedom, panel, str(db))
        freedom._execute_tool_chain(
            panel.task_id, panel, str(db), chain,
            threading.Event(), 1, panel.task_name,
        )
        joined = "\n".join(panel.logs)
        check("步骤执行完成", "sqlite_query" in joined and ("执行完成" in joined), panel.logs[-5:])
        check("params 真正下传（SQL 被使用）", any("select kind" in ln for ln in panel.logs), panel.logs[:6])
        check("只读白名单正常放行", not any("只读模式拒绝" in ln for ln in panel.logs))

        print("\n[2] 落盘产物（阶段3 依赖的老格式）")
        summary_file = round_dir / panel.task_name / f"{panel.task_name}_round_1.json"
        check("轮次汇总文件存在", summary_file.exists(), summary_file)
        summary = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.exists() else {}
        check("轮次汇总字段完整",
              summary.get("task_name") == panel.task_name
              and summary.get("round") == 1
              and "tools" in summary
              and "sqlite_query" in summary.get("tools", {}),
              list(summary.keys()))
        tools = summary.get("tools", {})
        first = (tools.get("sqlite_query") or [{}])[0]
        check("结果里保留输出行", bool(first.get("output_lines")), first.get("output_lines", [])[:2])

        slug_dirs = list((project / "latest").glob("*")) if (project / "latest").exists() else []
        check("latest/{slug}/round_1 目录生成", bool(slug_dirs) and (slug_dirs[0] / "round_1").exists(),
              [str(p) for p in slug_dirs])
        latest_files = list((slug_dirs[0] / "round_1").glob("sqlite_query_*.json")) if slug_dirs else []
        check("步骤级快照生成", bool(latest_files), latest_files)
        if latest_files:
            snap = json.loads(latest_files[0].read_text(encoding="utf-8"))
            check("快照状态为 success", snap.get("status") == "success", snap.get("status"))

        # 场景 2：阶段3 只读消费
        print("\n[3] 阶段3 读取路径未受影响")
        p3 = phase3.Phase3Manager(app)
        text = p3.get_round_summary_text(panel.task_name, 1)
        check("阶段3 能读到轮次摘要", "sqlite_query" in text and "成功" in text, text[:120])

        # 场景 3：凭证缺失
        print("\n[4] 凭证缺失时优雅失败")
        panel2 = StubPanel()
        chain2 = [{
            "name": "sqlite_query", "type": "tool",
            "params": json.dumps({"sql": "select 1", "password": {"$secret": "NO_SUCH_ENV_XYZ"}}),
            "inherit_from": None, "inherit_file": None,
        }]
        try:
            prep(freedom, panel2, str(db))
            freedom._execute_tool_chain(
                panel2.task_id, panel2, str(db), chain2,
                threading.Event(), 2, panel2.task_name,
            )
            crashed = False
        except Exception as e:
            crashed = True
            joined2 = str(e)
        check("凭证缺失不导致链路崩溃", not crashed, joined2 if crashed else "")
        joined2 = "\n".join(panel2.logs)
        check("凭证缺失有明确提示", "凭据解析失败" in joined2 and "NO_SUCH_ENV_XYZ" in joined2, panel2.logs[-5:])
        check("凭证缺失不泄漏明文", "s3cr3t" not in joined2)

        # 场景 4：只读拦截
        print("\n[5] 破坏性 SQL 被拦截")
        panel3 = StubPanel()
        prep(freedom, panel3, str(db))
        freedom._execute_tool_chain(
            panel3.task_id, panel3, str(db), build_chain("delete from events"),
            threading.Event(), 3, panel3.task_name,
        )
        joined3 = "\n".join(panel3.logs)
        check("写操作被拒", "只读模式拒绝执行" in joined3, panel3.logs[-4:])
        remaining = sqlite3.connect(db).execute("select count(*) from events").fetchone()[0]
        check("数据未被破坏", remaining == 3, remaining)

        # 场景 5：递归嵌套（挂起 → 唤醒）
        print("\n[6] 递归嵌套：父任务挂起与唤醒")
        child = ChatBotSkill(None)
        app.orchestrator.skills["chat_bot"] = child
        panel4 = StubPanel()
        panel4.task_name = "nest_parent"
        panel4.task_id = "nest_parent_1"
        nested_chain = [{
            "name": "chat_bot", "type": "skill",
            "params": json.dumps({"phase3_task": "child_panel"}),
            "inherit_from": None, "inherit_file": None,
        }]
        prep(freedom, panel4, str(db))
        worker = threading.Thread(
            target=lambda: freedom._execute_tool_chain(
                panel4.task_id, panel4, str(db), nested_chain,
                threading.Event(), 6, panel4.task_name,
            ),
            daemon=True,
        )
        worker.start()
        time.sleep(0.6)
        blocked = worker.is_alive()
        woke = child.kill_blocking_task("child_panel", json.dumps({"ok": True}))
        worker.join(10)
        check("父任务在子任务结束前保持挂起", blocked and woke, f"blocked={blocked} woke={woke}")
        check("唤醒后父任务退出", not worker.is_alive())
        logs4 = "\n".join(panel4.logs)
        check("嵌套日志齐全（挂起/唤醒）", "已挂起" in logs4 and "已唤醒" in logs4, panel4.logs[-4:])
        summary6 = round_dir / panel4.task_name / f"{panel4.task_name}_round_6.json"
        check("嵌套结果写入轮次汇总",
              summary6.exists() and '"output"' in summary6.read_text(encoding="utf-8"),
              str(summary6))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 58)
    print(f"结果：{passed}/{total} 通过")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}   {detail}")
    print("=" * 58)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
