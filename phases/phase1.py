"""phases/phase1.py - 一级执行台：单工具执行入口、实时日志转发。"""

import time
from core.task_executor import TaskExecutor
from datetime import datetime

class _ToolCallbacks:
    """适配器，将运行控制台的方法转换为 TaskExecutor 需要的回调形式"""

    def __init__(self, phase, host, log_target, log_target_program, store_tool_output, tool_registry=None):
        self.phase = phase
        self.host = host
        self.log_target = log_target
        self.log_target_program = log_target_program
        self.store_tool_output = store_tool_output
        self.current_tool = ""
        self.tool_registry = tool_registry
        self.start_time = None
        self._output_lines = []

    def on_log_line(self, host, line):
        """实时日志转发：直接透传给子页程序运行窗口"""
        self.log_target_program(host, f"[{self.current_tool}] {line}")
        self._output_lines.append(line)
        if hasattr(self.phase.app, 'structured_logger'):
            self.phase.app.structured_logger.log_tool_output(
                host=self.host,
                tool=self.current_tool,
                line=line
            )

    def on_tool_complete(self, host, tool_name, collected_lines):
        """工具完成回调"""
        duration = time.time() - self.start_time if self.start_time else 0
        status = "success"
        for line in collected_lines:
            if line.startswith("__ERROR__"):
                status = "failed"
                break

        if hasattr(self.phase.app, 'structured_logger'):
            self.phase.app.structured_logger.log_tool_end(
                host=host,
                tool=tool_name,
                status=status,
                duration=duration,
                result_summary=f"{len(collected_lines)} lines"
            )

        self.store_tool_output(host, tool_name, collected_lines)
        self.phase._handle_unknown(host, tool_name, collected_lines)
        self.log_target(host, f"✅ {tool_name} 执行完成")

class Phase1Collector:
    def __init__(self, app, executor=None):
        self.app = app
        self.executor = executor or TaskExecutor()

    def run_single_tool(self, host, project_dir, pause_event, tool_name,
                        log_target, log_target_program, store_tool_output,
                        params=None, timeout=None):
        """统一工具执行入口

        params: 传给工具的 --config（仅当工具声明支持时下发）
        timeout: 单步超时秒数（None 用全局默认 900s）
        """
        all_tools = self.app.tool_registry.get_all_tools()
        if tool_name not in all_tools:
            log_target(host, f"❌ 工具 {tool_name} 不存在")
            return

        start_time = time.time()

        if hasattr(self.app, 'structured_logger'):
            self.app.structured_logger.log_tool_start(
                host=host,
                tool=tool_name,
                args={}
            )

        callbacks = _ToolCallbacks(
            self, host, log_target, log_target_program,
            store_tool_output, tool_registry=self.app.tool_registry
        )

        callbacks.current_tool = tool_name
        callbacks.start_time = start_time
        log_target_program(host, f"🚀 启动 {tool_name} ...")

        self.executor.submit(
            target=host,
            tool_name=tool_name,
            project_dir=project_dir,
            callbacks=callbacks,
            params=params or {},
            pause_event=pause_event,
            timeout=timeout,
            arg_names=self.app.tool_registry.args_of(tool_name),
        )

    def _handle_unknown(self, host, tool_name, lines):
        msg = f"[{host}] {tool_name} 执行完成，原始输出已存储"
        self.app.root.after(0, lambda: self.app.summary_log.insert("end", msg + "\n"))
        self.app.root.after(0, lambda: self.app.summary_log.see('end'))
        self.app.log_global(msg, host=host)

    def kill_execution(self, host):
        self.executor.kill_by_host(host)