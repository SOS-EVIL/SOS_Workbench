"""core/task_executor.py - 工具调度。

提供并发上限（读界面上的并发数）、run_id 维度的进程句柄、统一超时。
submit() 是同步的：在调用者线程内执行完再返回。
"""

import queue
import threading
import time

from core.tool_runner import ToolRunner
from config.tools import TOOLS, DEFAULT_CONCURRENCY, DEFAULT_STEP_TIMEOUT

class TaskExecutor:
    def __init__(self, concurrency_getter=None, default_timeout=None, tool_resolver=None):
        self._runners = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._active = 0
        self._concurrency_getter = concurrency_getter
        self.default_timeout = default_timeout or DEFAULT_STEP_TIMEOUT
        self._tool_resolver = tool_resolver

    def _limit(self):
        if not self._concurrency_getter:
            return DEFAULT_CONCURRENCY
        try:
            return max(1, int(self._concurrency_getter()))
        except Exception:
            return DEFAULT_CONCURRENCY

    def active_count(self):
        with self._cond:
            return self._active

    def submit(self, *, target=None, tool_name=None, project_dir=None, callbacks=None,
               params=None, pause_event=None, cancel_event=None, timeout=None,
               arg_names=None, lock=None, run_id=None, host=None):
        target = target if target is not None else host
        if not target:
            raise ValueError("submit() 需要 target")
        if not tool_name:
            raise ValueError("submit() 需要 tool_name")

        run_id = run_id or f"{tool_name}@{target}#{int(time.time() * 1000)}"

        locked = False
        if lock:
            lock.acquire()
            locked = True
        try:
            with self._cond:
                while self._active >= self._limit():
                    self._cond.wait(0.2)
                self._active += 1
            try:
                return self._run_tool(
                    run_id=run_id, target=target, tool_name=tool_name,
                    project_dir=project_dir, callbacks=callbacks, params=params,
                    pause_event=pause_event, cancel_event=cancel_event,
                    timeout=timeout, arg_names=arg_names,
                )
            finally:
                with self._cond:
                    self._active -= 1
                    self._cond.notify_all()
        finally:
            if locked:
                try:
                    lock.release()
                except RuntimeError:
                    pass

    def _resolve_tool(self, tool_name):
        if self._tool_resolver:
            try:
                path = self._tool_resolver().get(tool_name)
                if path:
                    return path
            except Exception:
                pass
        return TOOLS.get(tool_name)

    def _run_tool(self, run_id, target, tool_name, project_dir, callbacks,
                  params, pause_event, cancel_event, timeout, arg_names):
        tool_path = self._resolve_tool(tool_name)
        if not tool_path:
            if callbacks:
                callbacks.on_log_line(target, f"__ERROR__:工具 {tool_name} 未注册")
            return {"exit_code": -1, "lines": [], "duration": 0.0, "stop_reason": "not_found"}

        runner = ToolRunner(
            tool_path, target, project_dir,
            params=params, timeout=timeout or self.default_timeout,
            run_id=run_id, arg_names=arg_names, tool_name=tool_name,
        )
        with self._lock:
            self._runners[run_id] = runner

        q = queue.Queue()
        reader = threading.Thread(
            target=runner.run, args=(q, pause_event, cancel_event), daemon=True
        )
        start = time.time()
        reader.start()

        collected = []
        while reader.is_alive() or not q.empty():
            try:
                line = q.get(timeout=0.1)
                if line.startswith("__EXIT__"):
                    continue
                collected.append(line)
                if callbacks:
                    callbacks.on_log_line(target, line)
            except queue.Empty:
                continue
        reader.join()

        with self._lock:
            self._runners.pop(run_id, None)

        if callbacks:
            callbacks.on_tool_complete(target, tool_name, collected)

        return {
            "exit_code": runner.proc.returncode if runner.proc else -1,
            "lines": collected,
            "duration": round(time.time() - start, 2),
            "stop_reason": runner._stop_reason or "finished",
        }

    def kill(self, run_id):
        with self._lock:
            runner = self._runners.get(run_id)
        if runner:
            runner.kill()
            return True
        return False

    def kill_by_target(self, target):
        with self._lock:
            runners = [r for r in self._runners.values() if r.target == target]
        for runner in runners:
            runner.kill()
        return len(runners)

    def kill_by_host(self, host):
        return self.kill_by_target(host)

    def active_runs(self):
        with self._lock:
            return {rid: r.tool_name for rid, r in self._runners.items()}
