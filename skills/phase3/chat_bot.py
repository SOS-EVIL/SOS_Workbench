"""skills/phase3/chat_bot.py - 三级示例技能：模型对话，支持被父任务嵌套调用。

递归契约见 递归标准.txt。
"""

import threading

from core.skill.base_skill import BaseSkill


class ChatBotSkill(BaseSkill):
    name = "chat_bot"
    description = "三级示例技能：同步调用模型并记录对话，支持被父任务嵌套调用"
    category = "phase3"

    BLOCK_TIMEOUT = 3600

    def __init__(self, toolbox):
        super().__init__(toolbox)
        self._block_events = {}
        self._block_results = {}
        self._block_lock = threading.Lock()

    @classmethod
    def can_handle(cls, user_msg):
        return False, {}

    def execute(self, **kwargs):
        if kwargs.get("blocking_mode"):
            return self._execute_blocking(
                kwargs.get("app"),
                kwargs.get("host", ""),
                kwargs.get("task_name", ""),
                kwargs.get("log_fn", lambda msg: None),
            )

        user_input = kwargs.get("user_input", "")
        app = kwargs.get("app")
        log_fn = kwargs.get("log_fn", lambda msg: None)
        host = kwargs.get("host") or self.current_host or "unknown"
        tracked = self.toolbox is not None and getattr(self.toolbox, "_app", None) is not None

        if tracked:
            self.log_start(host)

        if not app or not getattr(app, "chat_client", None):
            msg = "AI 客户端未初始化，请先配置 API Key"
            log_fn(f"[ERROR] {msg}")
            if tracked:
                self.log_failed(host, msg)
            return msg

        log_fn("[AI] 正在请求模型...")
        try:
            response = app.chat_client.send(user_input)
            if not response:
                response = "模型返回空内容"
            log_fn(f"[AI] {response}")
            if tracked:
                self.log_success(host, "完成")
            return response
        except Exception as e:
            log_fn(f"[ERROR] AI 调用失败: {e}")
            if tracked:
                self.log_failed(host, str(e))
            return f"AI 调用失败: {e}"

    def _execute_blocking(self, app, host, caller_task_name, log_fn):
        event = threading.Event()
        with self._block_lock:
            if caller_task_name in self._block_events:
                log_fn(f"[套娃] 拒绝重复登记 [{caller_task_name}]")
                return "无结果（重复嵌套）"
            self._block_events[caller_task_name] = event

        log_fn(f"[套娃] 已挂起，等待子面板 [{caller_task_name}] 结束")
        finished = event.wait(self.BLOCK_TIMEOUT)

        with self._block_lock:
            self._block_events.pop(caller_task_name, None)
            result = self._block_results.pop(caller_task_name, "无结果")

        if not finished:
            log_fn("[套娃] 等待超时")
            return "无结果（等待超时）"

        log_fn(f"[套娃] 已唤醒，结果长度 {len(str(result))}")
        return result

    def kill_blocking_task(self, task_name, result=""):
        with self._block_lock:
            event = self._block_events.get(task_name)
            if event is None:
                return False
            self._block_results[task_name] = result
        event.set()
        return True
