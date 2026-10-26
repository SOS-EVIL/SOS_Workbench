"""
phases/phase3.py - 决策台核心服务
提供三级 Skill 运行时的日志管理（启动清空 + 实时逐行写入）、
二级任务状态跟踪、轮次日志读取，以及模型自主死亡能力预留。
"""
import json
import os
import threading
from pathlib import Path
from datetime import datetime
from config.tools import PHASE3_BASE_DIR, ROUND_BASE_DIR

class Phase3Manager:
    def __init__(self, app):
        self.app = app
        self._death_flags = {}  # task_name -> bool
        self._file_locks = {}  # task_name -> threading.Lock   ← 加上这一行
        self._lock = threading.Lock()

    # 日志系统（启动清空 + 实时逐行写入）
    def get_chat_log_path(self, task_name: str) -> Path:
        task_dir = PHASE3_BASE_DIR / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir / "chat_log.json"

    def start_session(self, task_name: str):
        """清空旧日志，立即刷盘"""
        path = self.get_chat_log_path(task_name)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump([], f)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        self.clear_death(task_name)

    def append_chat_log(self, task_name: str, role: str, content: str):
        """追加对话记录，立即刷盘"""
        path = self.get_chat_log_path(task_name)
        with self._lock:
            if task_name not in self._file_locks:
                self._file_locks[task_name] = threading.Lock()
            file_lock = self._file_locks[task_name]

        with file_lock:
            log = []
            if path.exists():
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        log = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    log = []

            log.append({
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(timespec='seconds')
            })

            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(log, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass

    def load_chat_log(self, task_name: str) -> list:
        path = self.get_chat_log_path(task_name)
        if not path.exists():
            return []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    # 模型自主死亡能力（预留）
    def request_death(self, task_name: str):
        """由 Skill 调用：接收到模型死亡指令时设置标记"""
        with self._lock:
            self._death_flags[task_name] = True

    def is_dead(self, task_name: str) -> bool:
        with self._lock:
            return self._death_flags.get(task_name, False)

    def clear_death(self, task_name: str):
        with self._lock:
            self._death_flags[task_name] = False

    # 二级任务信息跟踪（动态获取，0.5秒级刷新）
    def get_linked_task_info(self, task_name: str) -> dict:
        """
        从 UI 面板实时获取关联的 Freedom 任务信息。
        返回：{"name": ..., "host": ..., "round": ..., "panel": ...} 或 None
        """
        for task_id, panel in self.app.phase3_panels.items():
            if panel.task_name == task_name:
                selected_task = panel.task_var.get()
                if not selected_task:
                    return None
                for fid, fp in self.app.freedom_panels.items():
                    if fp.task_name == selected_task:
                        return {
                            "name": fp.task_name,
                            "host": fp.selected_host_var.get(),
                            "round": fp.current_round,
                            "panel": fp
                        }
        return None

    # 轮次日志读取（供模型上下文使用）
    def get_round_log(self, freedom_task_name: str, round_num: int) -> dict:
        if not freedom_task_name or not ROUND_BASE_DIR:
            return {}
        file_path = ROUND_BASE_DIR / freedom_task_name / f"{freedom_task_name}_round_{round_num}.json"
        if not file_path.exists():
            return {}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def get_round_summary_text(self, freedom_task_name: str, round_num: int, max_chars=1500) -> str:
        data = self.get_round_log(freedom_task_name, round_num)
        if not data:
            return f"[第{round_num}轮无日志]"

        tools = data.get("tools", {})
        parts = [f"=== 二级任务 '{freedom_task_name}' 第{round_num}轮执行结果 ==="]
        for tool_name, results in tools.items():
            for idx, res in enumerate(results):
                status = "成功" if res.get("success") or res.get("status") == "success" else "失败"
                duration = res.get("duration", 0)
                summary = res.get("output", "") or res.get("result_summary", "")
                if len(summary) > 300:
                    summary = summary[:300] + "..."
                parts.append(f"  {tool_name}[{idx+1}]: {status} ({duration}s) - {summary}")
        full_text = "\n".join(parts)
        return full_text[:max_chars]