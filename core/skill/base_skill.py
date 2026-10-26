import json
from datetime import datetime

from core import targets as target_api

class BaseSkill:
    """所有 Skill 的基类（内置 实时最新全量日志 + 历史结构化日志）"""
    name = ""           # 唯一标识，如 "techstack"
    description = ""    # 简短描述
    category = ""       # 分类

    def __init__(self, toolbox):
        self.toolbox = toolbox
        # 核心：缓冲区 = 存储【本次Skill运行的所有日志行】（全量）
        self._log_buffer = []
        self.start_time = 0
        self.current_host = None
        # 运行中快照节流：终态（success/failed）永远立即写盘
        self._save_interval = 0.3
        self._last_save = 0.0

    def log_start(self, host, **kwargs):
        """技能启动：清空缓冲区（只保留本次运行）+ 计时 + 实时写入running状态"""
        self._log_buffer.clear()
        self.start_time = datetime.now().timestamp()
        self._last_save = 0.0          # 新一轮不受上一轮节流窗口影响
        self.current_host = host

        self.log_to_subpage(host, f"🚀 技能启动：{self.name}")
        self._save_latest(host, status="running")

    def log_to_subpage(self, host, msg):
        """输出到UI + 追加到【本次全量缓冲区】 + 实时刷新latest文件"""
        self.toolbox.log_to_subpage(host, msg)
        self._log_buffer.append({
            "ts": datetime.now().isoformat(timespec='seconds'),
            "line": msg
        })
        self._save_latest(host, status="running")

    def log_success(self, host, message="执行完成"):
        """技能成功：写入最终全量日志 + 标记success状态
        注意：调用前应将所有需要记录的 log_to_subpage 输出完毕，
        否则最后一条输出会丢失。
        """
        duration = round(datetime.now().timestamp() - self.start_time, 2)
        self._flush_logs(host, status="success", duration=duration, message=message)

    def log_failed(self, host, message="执行失败"):
        """技能失败：写入最终全量日志 + 标记failed状态"""
        duration = round(datetime.now().timestamp() - self.start_time, 2)
        self._flush_logs(host, status="failed", duration=duration, message=message)

    def _flush_logs(self, host, status, duration=0, message=""):
        if not self.toolbox._app or not self.toolbox._app.structured_logger:
            return

        events = [{
            "event": "skill_start",
            "host": host,
            "skill": self.name,
            "ts": datetime.fromtimestamp(self.start_time).isoformat(timespec='seconds')
        }]
        for item in self._log_buffer:
            events.append({
                "event": "skill_output",
                "host": host,
                "skill": self.name,
                "line": item["line"],
                "ts": item["ts"]
            })
        events.append({
            "event": "skill_end",
            "host": host,
            "skill": self.name,
            "status": status,
            "duration": duration,
            "result_summary": message,
            "ts": datetime.now().isoformat(timespec='seconds')
        })
        for ev in events:
            self.toolbox._app.structured_logger.log_event(ev)

        # 最终更新latest：保存本次运行的完整日志
        self._save_latest(host, status=status)

    # 实时最新全量日志（核心：本次运行所有内容）
    def _save_latest(self, host, status):
        project_dir = getattr(self.toolbox._app, 'project_dir', None) if self.toolbox._app else None
        if not project_dir:
            return

        # 运行中节流：避免每写一行日志就全量重写一次 JSON（O(n²)）
        now = datetime.now().timestamp()
        if status == "running" and (now - self._last_save) < self._save_interval:
            return
        self._last_save = now

        host_safe = target_api.slug(host)
        latest_dir = project_dir / "latest" / host_safe
        latest_dir.mkdir(parents=True, exist_ok=True)
        latest_file = latest_dir / f"{self.name}.json"

        # 状态保护：如果文件已经是终态，不再被 running 覆盖
        if status == "running" and latest_file.exists():
            try:
                with open(latest_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if existing.get("status") in ("success", "failed"):
                    return  # 已经是终态，拒绝 running 覆盖
            except Exception:
                pass

        data = {
            "skill": self.name,
            "host": host,
            "status": status,
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(timespec='seconds'),
            "last_update": datetime.now().isoformat(timespec='seconds'),
            "full_output": self._log_buffer.copy()
        }

        try:
            with open(latest_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def execute(self, **kwargs):
        raise NotImplementedError("子类必须实现 execute 方法")

    def resume_with_input(self, user_msg, **kwargs):
        raise NotImplementedError("需要等待输入的 Skill 必须实现 resume_with_input 方法")