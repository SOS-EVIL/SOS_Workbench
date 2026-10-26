"""core/skill/skill_toolbox.py - 技能沙箱：UI 输出、工具调用、跨技能内存。"""

import sys
import time
import threading

class SkillToolbox:
    """Skill 工具箱，封装所有 Skill 可以调用的系统能力"""

    def __init__(self, app):
        self._app = app
        self._subpage_creation_lock = threading.Lock()

    def output(self, sender, msg):
        """输出消息到聊天框（线程安全）"""
        if self._app and self._app.root:
            self._app.root.after(0, lambda: self._app.display_chat(sender, msg))

    def get_subpages(self):
        """获取当前项目所有目标的 host 列表（线程安全）"""
        if not self._app:
            return []
        with self._app.targets_lock:
            hosts = [t.get("host") for t in self._app.targets if t.get("host")]
        return hosts

    def ensure_subpage(self, host):
        """确保目标存在，如果不存在则异步创建（无死锁版本）
        核心原则：后台线程永远不持有锁等待UI线程
        """
        if not self._app:
            return False

        # 第一步：快速检查是否已存在（无锁，快速路径）
        with self._app.targets_lock:
            if any(t.get("host") == host for t in self._app.targets):
                return True

        # 第二步：获取创建锁，防止多线程同时创建同一个子页
        with self._subpage_creation_lock:
            # 双重检查：防止其他线程已经创建完成
            with self._app.targets_lock:
                if any(t.get("host") == host for t in self._app.targets):
                    return True

            # 异步提交创建任务到UI线程（不等待，立即返回）
            self._app.root.after(0, lambda: self._app.add_manual_subpage_by_host(host))

        # 第三步：后台线程轮询等待创建完成（不持有任何锁）
        start_time = time.time()
        while time.time() - start_time < 3.0:  # 最多等待3秒
            with self._app.targets_lock:
                if host in self._app.target_program_logs:
                    # 子页已完全创建并注册到日志字典
                    time.sleep(0.05)  # 额外等待UI渲染完成
                    return True
            time.sleep(0.1)

        return False

    def run_tool(self, host, tool_name):
        """对指定目标运行工具（线程安全）"""
        if not self._app:
            return False
        return self._app.run_single_tool_for_host(host, tool_name)

    def get_tool_list(self):
        """获取可用工具名列表（双源兼容）"""
        if hasattr(self._app, 'tool_registry') and self._app.tool_registry:
            return list(self._app.tool_registry.get_all_tools().keys())
        if hasattr(self._app, 'phase1') and self._app.phase1:
            return self._app.phase1.tools
        return []

    def get_tool_info_map(self):
        """获取 {工具名: 描述} 字典，描述来自工具的 manifest。"""
        tool_names = self.get_tool_list()
        registry = getattr(self._app, "tool_registry", None)
        desc_map = {}
        for name in tool_names:
            desc = ""
            if registry:
                info = registry.get_tool_info(name)
                desc = info.get("description", "") if info else ""
            desc_map[name] = desc or name
        return desc_map

    def ai_call(self, prompt):
        """调用 AI 模型并返回字符串结果（增强错误处理）"""
        if not self._app or not self._app.chat_client:
            self.output("系统", "❌ AI模型未初始化，请先配置API密钥")
            return None
        try:
            resp = self._app.chat_client.send(prompt)
            if not resp:
                self.output("系统", "❌ AI模型返回空响应")
                return None
            return resp.strip()
        except Exception as e:
            error_msg = f"❌ AI调用失败: {str(e)}"
            print(f"[SkillToolbox] AI调用异常: {error_msg}", file=sys.stderr)
            self.output("系统", error_msg)
            return None

    def log_to_subpage(self, host, msg):
        """将消息输出到指定子页的“程序运行情况”窗口（线程安全）"""
        if not self._app:
            return
        # 先检查是否存在，避免不必要的锁操作
        with self._app.targets_lock:
            if host in self._app.target_program_logs:
                self._app.log_target_program(host, msg)
            else:
                # 子页不存在时输出到全局日志
                self._app.log_global(f"[{host}] {msg}", host=host)

    def add_to_memory(self, host, info):
        """将结构化数据存入全局内存（供后续 Skill 跨调用使用）"""
        if self._app and self._app.orchestrator:
            self._app.orchestrator.add_to_memory(host, info)

    def log_skill_event(self, host, event_type, **details):
        """记录技能事件到结构化日志（写入 _skill.jsonl）"""
        if not self._app or not hasattr(self._app, 'structured_logger') or not self._app.structured_logger:
            return
        self._app.structured_logger.log_skill_event(host, event_type, **details)

    def log_skill_start(self, host, skill_name, **kwargs):
        """技能开始"""
        self.log_skill_event(host, "start", skill=skill_name, **kwargs)

    def log_skill_wait(self, host, skill_name, prompt):
        """技能等待用户输入"""
        self.log_skill_event(host, "wait", skill=skill_name, prompt=prompt)

    def log_skill_resume(self, host, skill_name, user_msg):
        """技能恢复执行（收到用户输入）"""
        self.log_skill_event(host, "resume", skill=skill_name, user_msg=user_msg)

    def log_skill_end(self, host, skill_name, status, duration=0):
        """技能结束"""
        self.log_skill_event(host, "end", skill=skill_name, status=status, duration=duration)