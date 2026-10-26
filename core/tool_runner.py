"""core/tool_runner.py - 子进程管理。

平台契约（语言无关）：

    <任何可执行工具> --project-dir <dir> --target <target> [--config <json>]

stderr 逐行日志，stdout 用 __JSON_RESULT_START__ / __JSON_RESULT_END__ 包裹单个 JSON。
支持暂停、取消、超时，并强制终止整个进程树。
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# 不传给工具子进程的平台凭据。工具如果需要模型，应通过显式参数自行获取。
_CHILD_ENV_DENYLIST = ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL")


def child_env(extra=None):
    """构造工具子进程环境：复制当前环境，但移除平台注入的凭据。"""
    env = os.environ.copy()
    for name in _CHILD_ENV_DENYLIST:
        env.pop(name, None)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


class ToolRunner:
    def __init__(self, tool_path, target, project_dir, params=None,
                 timeout=None, run_id=None, arg_names=None, tool_name=""):
        self.tool_path = tool_path
        self.target = target
        self.project_dir = project_dir
        self.params = params or {}
        self.timeout = float(timeout) if timeout else None
        self.run_id = run_id or f"{tool_name}@{target}"
        self.arg_names = frozenset(arg_names) if arg_names else frozenset()
        self.tool_name = tool_name

        self.proc = None
        self._stop_reason = None
        self._stop_lock = threading.Lock()
        self._reason_reported = False

    def _launcher(self):
        """按后缀决定怎么启动，使工具可以是任意语言。"""
        path = Path(self.tool_path)
        suffix = path.suffix.lower()

        if suffix in (".py", ".pyz"):
            return [sys.executable, str(path)]
        if suffix in (".bat", ".cmd"):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(path)]
        if suffix == ".ps1":
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
        if suffix == ".js":
            return [shutil.which("node") or "node", str(path)]
        if suffix == ".sh":
            return [(shutil.which("bash") or "sh"), str(path)]
        return [str(path)]

    def build_command(self):
        names = self.arg_names
        legacy = not names
        cmd = self._launcher()

        if legacy or "--project-dir" in names:
            cmd += ["--project-dir", str(self.project_dir)]
        if "--target" in names:
            cmd += ["--target", str(self.target)]
        if legacy or "--host" in names:
            cmd += ["--host", str(self.target)]

        if self.params:
            if "--config" in names:
                cmd += ["--config", json.dumps(self.params, ensure_ascii=False)]
            elif not legacy:
                # 兼容层：没有 --config 的工具，把 params 的同名键映射成它自己的参数
                for key, value in self.params.items():
                    flag = "--" + str(key).strip().replace("_", "-")
                    if flag not in names or flag in ("--project-dir", "--target", "--host"):
                        continue
                    if isinstance(value, bool):
                        if value:
                            cmd.append(flag)
                    elif value is not None and not isinstance(value, (dict, list)):
                        cmd += [flag, str(value)]
        return cmd

    def run(self, output_queue, pause_event=None, cancel_event=None):
        try:
            env = child_env({
                "TARGET": str(self.target),
                "PROJECT_DIR": str(self.project_dir),
                "RUN_ID": str(self.run_id),
                "PYTHONUNBUFFERED": "1",
            })

            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            proc = subprocess.Popen(
                self.build_command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(self.project_dir) if self.project_dir else None,
                **kwargs,
            )
            self.proc = proc

            deadline = (time.time() + self.timeout) if self.timeout else None
            threading.Thread(
                target=self._watch, args=(deadline, cancel_event, pause_event), daemon=True
            ).start()

            for line in iter(proc.stdout.readline, ""):
                if pause_event is not None:
                    while pause_event.is_set() and not self._is_stopped():
                        time.sleep(0.15)
                output_queue.put(line.rstrip())

            try:
                proc.stdout.close()
            except Exception:
                pass

            code = proc.wait()
            self._report_stop_reason(output_queue)
            output_queue.put(f"__EXIT__:{code}")

        except Exception as e:
            output_queue.put(f"__ERROR__:{type(e).__name__}: {e}")
            output_queue.put("__EXIT__:-1")
        finally:
            if self.proc is not None and self.proc.poll() is None:
                self._kill_tree()

    def _watch(self, deadline, cancel_event, pause_event=None):
        while True:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            if pause_event is not None and pause_event.is_set():
                time.sleep(0.2)
                continue
            if cancel_event is not None and cancel_event.is_set():
                self._set_reason("cancelled")
                self._kill_tree()
                return
            if deadline is not None and time.time() > deadline:
                self._set_reason("timeout")
                self._kill_tree()
                return
            time.sleep(0.2)

    def _set_reason(self, reason):
        with self._stop_lock:
            if self._stop_reason is None:
                self._stop_reason = reason

    def _is_stopped(self):
        return self._stop_reason is not None

    def _report_stop_reason(self, output_queue):
        with self._stop_lock:
            if self._reason_reported:
                return
            reason = self._stop_reason
            self._reason_reported = True
        if reason == "timeout":
            output_queue.put(f"__ERROR__:执行超时（{self.timeout:.0f}s），已强制终止进程树")
        elif reason == "cancelled":
            output_queue.put("__ERROR__:执行已取消")
        elif reason == "killed":
            output_queue.put("__ERROR__:执行被强制终止")

    def _kill_tree(self):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                taskkill = shutil.which("taskkill") or "taskkill"
                subprocess.run(
                    [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, text=True, errors="replace", timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def kill(self):
        self._set_reason("killed")
        self._kill_tree()

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None
