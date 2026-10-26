"""
core/agent_orchestrator.py - AI 编排器核心
负责 Skill 调度、动作执行和对话管理
"""

import os
import json
import re
import sys
import importlib
import importlib.util
import threading
from pathlib import Path
from core.deepseek_client import DeepSeekClient
from core.skill.base_skill import BaseSkill
from core.skill.skill_toolbox import SkillToolbox

class AgentOrchestrator:
    def __init__(self, client: DeepSeekClient, on_message=None, app=None):
        self.client = client  # AI客户端
        self.actions = {}
        self.memory = {}
        self.conversation_history = []
        self.on_message = on_message
        self.app = app

        self.toolbox = SkillToolbox(self.app) if self.app else None

        from core.shell_executor import ShellExecutor
        self.shell = ShellExecutor()

        self.skills = {}
        self._load_skills()

        self._pending_skill = None      # 当前等待输入的 Skill 实例
        self._pending_kwargs = None     # 调用 execute 时的原始 kwargs
        self._pending_state = None      # Skill 自定义的状态变量
        self._pending_lock = threading.Lock()  # 线程安全锁

    def _load_skills(self):
        """扫描仓库 skills/ 与托管 skills/ 并实例化 Skill。

        仓库目录仍用包导入；SOS_DATA_DIR/skills 是 CLI 审核通过的托管技能，
        用文件路径导入，不需要改仓库或补 __init__.py。
        """
        from config.tools import BASE_DIR, SKILLS_DIRS

        categories = ["recon", "vuln", "exploit", "others", "atomic", "analysis", "phase3"]
        for base in SKILLS_DIRS:
            for category in categories:
                abs_dir = Path(base) / category
                if not abs_dir.is_dir():
                    continue
                is_repo = Path(base) == Path(BASE_DIR)
                if is_repo:
                    self._ensure_package(str(abs_dir))
                for filename in os.listdir(abs_dir):
                    if not filename.endswith(".py") or filename == "__init__.py":
                        continue
                    if is_repo:
                        module_name = f"skills.{category}.{filename[:-3]}"
                        try:
                            module = importlib.import_module(module_name)
                        except Exception as e:
                            print(f"[Agent] 加载 {module_name} 失败: {e}", file=sys.stderr)
                            continue
                    else:
                        module_name = f"sos_skills.{category}.{filename[:-3]}"
                        try:
                            spec = importlib.util.spec_from_file_location(
                                module_name, abs_dir / filename
                            )
                            module = importlib.util.module_from_spec(spec)
                            sys.modules[module_name] = module
                            spec.loader.exec_module(module)
                        except Exception as e:
                            print(f"[Agent] 加载 {module_name} 失败: {e}", file=sys.stderr)
                            continue
                    for attr_name in dir(module):
                        cls = getattr(module, attr_name)
                        if (isinstance(cls, type) and
                            issubclass(cls, BaseSkill) and
                            cls != BaseSkill):
                            skill_instance = cls(self.toolbox)
                            self.skills[skill_instance.name] = skill_instance
                            print(f"[Agent] 加载 Skill: {skill_instance.name}", file=sys.stderr)

    @staticmethod
    def _ensure_package(directory):
        """为已存在的技能目录补齐 __init__.py（含各级父目录），不新建目录、不碰项目根。"""
        from config.tools import BASE_DIR
        root = os.path.abspath(str(BASE_DIR))
        current = os.path.abspath(directory)
        while current != root and current.startswith(root) and os.path.isdir(current):
            init_file = os.path.join(current, "__init__.py")
            if not os.path.exists(init_file):
                try:
                    open(init_file, "w").close()
                except OSError:
                    pass
            current = os.path.dirname(current)

    def _run_skill_instance(self, skill_instance, kwargs):
        try:
            result = skill_instance.execute(**kwargs)
            if isinstance(result, tuple) and len(result) >= 2 and result[0] == "__WAIT__":
                prompt = result[1]
                state = result[2] if len(result) > 2 else None
                with self._pending_lock:
                    self._pending_skill = skill_instance
                    self._pending_kwargs = kwargs
                    self._pending_state = state
                if self.on_message:
                    self.on_message("系统", prompt)
                return ""
            else:
                return result
        except Exception as e:
            return f"❌ Skill 执行失败: {str(e)}"

    # 外部入口（核心修改点）
    def run_autonomous(self, user_msg, context=""):
        # 1. 优先处理等待输入的 Skill（线程安全）
        with self._pending_lock:
            if self._pending_skill:
                skill = self._pending_skill
                kwargs = self._pending_kwargs
                state = self._pending_state
                # 立即清除等待状态，防止重复处理
                self._pending_skill = None
                self._pending_kwargs = None
                self._pending_state = None

                # 调用恢复方法，传入用户消息和状态
                try:
                    if state:
                        result = skill.resume_with_input(user_msg, state=state, **kwargs)
                    else:
                        result = skill.resume_with_input(user_msg, **kwargs)

                    # 检查是否需要继续等待（支持多步输入）
                    if isinstance(result, tuple) and len(result) >= 2 and result[0] == "__WAIT__":
                        prompt = result[1]
                        new_state = result[2] if len(result) > 2 else None
                        with self._pending_lock:
                            self._pending_skill = skill
                            self._pending_kwargs = kwargs
                            self._pending_state = new_state
                        if self.on_message:
                            self.on_message("系统", prompt)
                        return ""
                    else:
                        if self.on_message:
                            self.on_message("系统", result)
                        return result
                except Exception as e:
                    error_msg = f"❌ Skill 恢复执行失败: {str(e)}"
                    if self.on_message:
                        self.on_message("系统", error_msg)
                    return error_msg

        # 2. 原有遍历 Skills 的逻辑
        for skill_name, skill_instance in self.skills.items():
            try:
                can_handle, kwargs = skill_instance.can_handle(user_msg)
                if can_handle:
                    kwargs.setdefault("raw_msg", user_msg)
                    result = self._run_skill_instance(skill_instance, kwargs)
                    if self.on_message and result:  # 只有非空结果才输出
                        self.on_message("系统", result)
                    return result
            except Exception as e:
                print(f"[Agent] Skill {skill_name} 出错: {e}", file=sys.stderr)
                continue

        # 3. 无技能处理 → 弹窗确认
        def ask():
            if self.app and self.app.root:
                from tkinter import messagebox          # 延迟导入：core 层保持无 Tk 依赖
                ans = messagebox.askyesno(
                    "未命中 Skill",
                    f"未识别到对应技能，是否发送给AI模型？\n\n消息：{user_msg[:100]}",
                    parent=self.app.root
                )
                if ans:
                    self._chat_with_model(user_msg, context)
                else:
                    if self.on_message:
                        self.on_message("系统", "已取消。可以直接描述你的需求，或先查看可用工具。")
            else:
                self._chat_with_model(user_msg, context)

        if self.app and self.app.root:
            self.app.root.after(0, ask)
        else:
            ask()
        return "等待用户确认..."

    def _chat_with_model(self, user_msg, context):
        reply, action_taken = self.process_message(user_msg, context)
        if self.on_message:
            self.on_message("AI" if not action_taken else "系统", reply)
        return reply

    def register_action(self, name, func, description=""):
        self.actions[name] = (func, description)

    def _build_system_prompt(self, available_tools=None):
        actions_desc = "\n".join([f"- {name}: {desc}" for name, (func, desc) in self.actions.items()])
        tools_section = f"\n[SYSTEM] 当前可用工具：{', '.join(available_tools)}" if available_tools else ""
        return f"""
[系统] 你是任务编排助手，负责把用户需求转成对工具与目标的调用。
- 仅在下述动作范围内做出选择，不编造不存在的工具。
- 用户要求批量操作时，先确认目标范围再执行。
- 常规对话正常应答。

可用动作：
{actions_desc}
{tools_section}
"""

    def process_message(self, user_msg, context="", available_tools=None):
        # 检查AI客户端是否初始化成功
        if not self.client or not self.client.client:
            return "❌ AI模型未初始化：请配置API密钥后重试", False

        system_prompt = self._build_system_prompt(available_tools)
        msgs = [{"role": "system", "content": system_prompt}]
        if context:
            msgs.append({"role": "user", "content": f"背景：{context}"})
        self.conversation_history.append({"role": "user", "content": user_msg})
        msgs.extend(self.conversation_history[-5:])

        try:
            # 双重防护，防止客户端调用崩溃
            resp = self.client.client.chat.completions.create(
                model=self.client.model,
                messages=msgs,
                max_tokens=500,
                temperature=0.2
            )
            reply = resp.choices[0].message.content.strip()
        except Exception as e:
            return f"模型调用失败：{str(e)}", False

        action_match = re.search(r'\{.*\}', reply, re.DOTALL)
        if action_match:
            json_str = action_match.group()
            action_data = None
            try:
                action_data = json.loads(json_str)
            except Exception:
                try:
                    action_data = json.JSONDecoder().raw_decode(json_str)[0]
                except Exception:
                    pass

            if action_data:
                action_name = action_data.get("action")
                args = action_data.get("args", {})
                if action_name in self.actions:
                    try:
                        res = self.actions[action_name][0](**args)
                        self.conversation_history.append({"role": "assistant", "content": reply})
                        output = f"操作 {action_name} 完成：{str(res)[:300]}"
                        self.conversation_history.append({"role": "assistant", "content": output})
                        return output, True
                    except Exception as e:
                        return f"动作执行失败: {str(e)}", False
                return f"未知操作: {action_name}", False

        self.conversation_history.append({"role": "assistant", "content": reply})
        return reply, False

    def add_to_memory(self, host, info):
        if host not in self.memory:
            self.memory[host] = []
        self.memory[host].append(info)