import json
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

class StructuredLogger:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.project_dir: Optional[Path] = None
        self.log_dir: Optional[Path] = None
        self.queue = queue.Queue()
        self.running = True
        self.worker = threading.Thread(target=self._write_loop, daemon=True)
        self.worker.start()
        self._file_lock = threading.Lock()

    def set_project_dir(self, project_dir: Path):
        """设置当前项目目录，并创建日志根目录"""
        self.project_dir = project_dir
        self.log_dir = project_dir / "structured_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_tool_start(self, host: str, tool: str, **args):
        self.log_event({
            "event": "tool_start",
            "host": host,
            "tool": tool,
            "args": args
        })

    def log_tool_output(self, host: str, tool: str, line: str):
        self.log_event({
            "event": "tool_output",
            "host": host,
            "tool": tool,
            "line": line
        })

    def log_tool_end(self, host: str, tool: str, status: str, duration: float, result_summary: str = ""):
        event = {
            "event": "tool_end",
            "host": host,
            "tool": tool,
            "status": status,
            "duration": round(duration, 2)
        }
        if result_summary:
            event["result_summary"] = result_summary
        self.log_event(event)

    def log_skill_event(self, host: str, event_type: str, **details):
        """通用技能事件记录，event_type 会拼接为 skill_{event_type}"""
        event = {"event": f"skill_{event_type}", "host": host, **details}
        self.log_event(event)

    def log_event(self, event: dict):
        if not self.log_dir:
            print("[StructuredLogger] 未设置项目目录，忽略日志", file=sys.stderr)
            return
        self.queue.put(event)

    def _write_loop(self):
        while self.running:
            try:
                event = self.queue.get(timeout=1)
                self._write_event(event)
            except queue.Empty:
                continue

    def _sanitize_host(self, host: str) -> str:
        from core import targets as target_api
        return target_api.slug(host)

    def _get_tool_file(self, host: str, tool: str) -> Path:
        host_dir = self.log_dir / self._sanitize_host(host)
        host_dir.mkdir(exist_ok=True)
        return host_dir / f"{tool}.jsonl"

    def _get_summary_file(self, host: str) -> Path:
        host_dir = self.log_dir / self._sanitize_host(host)
        host_dir.mkdir(exist_ok=True)
        return host_dir / "_summary.jsonl"

    # 【核心修改】每个 Skill 独立日志文件
    def _get_skill_file(self, host: str, skill_name: str) -> Path:
        """根据 Skill 名称生成独立文件：如 techstack.jsonl、atomic.jsonl"""
        host_dir = self.log_dir / self._sanitize_host(host)
        host_dir.mkdir(exist_ok=True)
        return host_dir / f"{skill_name}.jsonl"

    def _get_global_file(self) -> Path:
        return self.log_dir / "_global.jsonl"

    def _write_event(self, event: dict):
        event_type = event.get("event")
        host = event.get("host")
        if event_type in ("tool_start", "tool_output", "tool_end", "tool_error"):
            tool = event.get("tool")
            if not tool or not host:
                return
            file_path = self._get_tool_file(host, tool)

        elif event_type in ("scan_start", "scan_end"):
            if not host:
                return
            file_path = self._get_summary_file(host)

        # 【核心修改】Skill 独立文件写入
        elif event_type.startswith("skill_"):
            skill = event.get("skill")
            if not skill or not host:
                file_path = self._get_global_file()
            else:
                # 每个 Skill 单独一个文件，和工具完全一致
                file_path = self._get_skill_file(host, skill)

        else:
            file_path = self._get_global_file()

        if "ts" not in event:
            event["ts"] = datetime.now().isoformat(timespec='seconds')

        with self._file_lock:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def close(self):
        self.running = False
        self.worker.join(timeout=2)

    @classmethod
    def get_instance(cls):
        return cls()