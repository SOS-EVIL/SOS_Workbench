"""core/headless.py - CLI / 测试用的无 GUI 宿主。

CLI 复用 Phase2Freedom 的编排逻辑，但不启动 Tk：
    - HeadlessRoot  : 把 root.after 变成同步调用
    - HeadlessPanel : 替代 Tk 面板，只收集日志与轮次
    - HeadlessApp   : 提供 Phase2Freedom 需要的 app 接口

不改变 ToolRunner / TaskExecutor / Phase2 的主流程。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from core import targets as target_api
from core.agent_orchestrator import AgentOrchestrator
from core.log_writer import LogWriter
from core.structured_logger import StructuredLogger
from core.task_executor import TaskExecutor
from core.tool_registry import ToolRegistry
from utils.log_utils import timestamp


class HeadlessRoot:
    """没有 Tk mainloop：after 直接执行，保证 CLI 顺序可控。"""

    def after(self, delay, fn, *args, **kwargs):
        fn(*args, **kwargs)


class HeadlessPanel:
    """Phase2Freedom 需要的最小面板。"""

    def __init__(self, task_name, task_id, round_num=1, plan=None):
        self.task_name = task_name
        self.task_id = task_id
        self.current_round = round_num
        self.plan = plan or {"tools": []}
        self.logs = []

    def append_log(self, msg):
        self.logs.append(str(msg))

    def advance_round(self):
        self.current_round += 1

    def get_tool_boxes_data(self):
        return self.plan


class _HeadlessPhase1:
    """Phase2 只用到 kill_execution；真正的工具执行走 TaskExecutor。"""

    def __init__(self, executor):
        self.executor = executor

    def kill_execution(self, host):
        return self.executor.kill_by_target(host)


class HeadlessApp:
    """Phase2Freedom 的无 GUI 宿主。"""

    def __init__(self, project_dir, target, tool_dirs=None, load_skills=True):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.project_name = self.project_dir.name
        self.target = str(target)
        self.root = HeadlessRoot()

        self.targets = [{
            "target": self.target,
            "host": self.target,
            "kind": target_api.kind_of(self.target),
            "ip": "",
            "is_alive": True,
            "source": "cli",
        }]
        self.targets_lock = threading.RLock()
        self.target_logs = {}
        self.target_program_logs = {self.target: []}
        self.target_buttons = {}
        self.target_pause_events = {}
        self.target_threads = {}
        self.collected_data = {}
        self.recent_log = []

        self.tool_registry = ToolRegistry(tool_dirs=tool_dirs)
        self.executor = TaskExecutor(
            concurrency_getter=lambda: 1,
            tool_resolver=lambda: self.tool_registry.get_all_tools(),
        )
        self.phase1 = _HeadlessPhase1(self.executor)
        self.freedom_panels = {}
        self.phase3_panels = {}
        self.phase3_manager = None
        self.chat_client = None

        self.structured_logger = StructuredLogger.get_instance()
        self.structured_logger.set_project_dir(self.project_dir)
        self._log_writer = LogWriter(lambda: self.project_dir)
        self.orchestrator = (
            AgentOrchestrator(None, on_message=self._on_message, app=self)
            if load_skills else SimpleNamespace(skills={})
        )

    def _on_message(self, sender, text):
        self._log_writer.write(f"[{timestamp()}] [{sender}] {text}\n")

    def _log_line(self, msg, host=None):
        stamp = timestamp()
        if host:
            return f"[{stamp}] [{host}] {msg}\n"
        return f"[{stamp}] {msg}\n"

    def log_global(self, msg, host=None, sensitive=False):
        self._log_writer.write(self._log_line(msg, host))
        if host:
            self._log_to_target(host, msg)

    def log_target(self, host, msg):
        line = self._log_line(msg, host)
        self._log_to_target(host, line)
        self._log_writer.write(line)

    def log_target_program(self, host, msg):
        line = self._log_line(msg, host)
        with self.targets_lock:
            self.target_program_logs.setdefault(host, []).append(line)
        self._log_writer.write(line)

    def _log_to_target(self, host, line):
        with self.targets_lock:
            self.target_logs.setdefault(host, []).append(line)

    def store_tool_output(self, host, tool_name, lines):
        with self.targets_lock:
            self.collected_data.setdefault(host, {})[tool_name] = list(lines)

    # Phase2Freedom 的任务注册钩子：CLI 单任务，无需额外状态
    def register_freedom_thread(self, host, pause_event):
        self.target_pause_events[host] = pause_event

    def unregister_freedom_thread(self, host):
        self.target_pause_events.pop(host, None)

    # SkillToolbox 可能调用到的 UI 方法
    def display_chat(self, sender, text):
        self._on_message(sender, text)

    def add_manual_subpage_by_host(self, host):
        return f"CLI 模式下目标 {host} 已存在"

    def close(self):
        try:
            self._log_writer.close()
        except Exception:
            pass
        try:
            self.structured_logger.close()
        except Exception:
            pass

    @property
    def session_log_path(self):
        return self.project_dir / "session.log"

    @property
    def structured_logs_dir(self):
        return self.project_dir / "structured_logs"
