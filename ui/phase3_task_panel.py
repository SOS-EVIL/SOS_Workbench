"""
ui/phase3_task_panel.py - 决策台任务面板（单个计划任务）
布局：Skill选择置顶 | 关联任务 + 轮次选择 | 自动化模式（鲸吞/栖鲸）| 输入框 + 发送按钮 | 循环日志
"""
from tkinter import Frame, Label, Button, StringVar, scrolledtext, ttk, Text, Scrollbar, Entry, Checkbutton, BooleanVar
import threading
import json

from config.tools import ENABLE_AUTO_MODE

class Phase3TaskPanel(Frame):
    def __init__(self, parent, app, task_name, get_hosts_fn):
        super().__init__(parent)
        self.app = app
        self.task_name = task_name
        self.get_hosts_fn = get_hosts_fn
        self.selected_host = ""
        self.selected_round = 1

        control_bar = Frame(self)
        control_bar.pack(side="top", fill="x", padx=5, pady=(2, 0))

        Label(control_bar, text="三级Skill:", font=("微软雅黑", 9)).pack(side="left", padx=(0, 5))
        self.skill_var = StringVar()
        self.skill_combo = ttk.Combobox(control_bar, textvariable=self.skill_var,
                                        state="readonly", width=15)
        self.skill_combo.pack(side="left", padx=(0, 10))
        self.skill_combo.bind("<Button-1>", lambda e: self._load_phase3_skills())

        Label(control_bar, text="关联任务:", font=("微软雅黑", 9)).pack(side="left", padx=(10, 5))
        self.task_var = StringVar()
        self.task_combo = ttk.Combobox(control_bar, textvariable=self.task_var,
                                       state="readonly", width=12)
        self.task_combo.pack(side="left", padx=(0, 5))
        self.task_combo.bind("<<ComboboxSelected>>", self._on_task_selected)
        self.task_combo.bind("<Button-1>", lambda e: self._load_freedom_tasks())

        Label(control_bar, text="轮次:", font=("微软雅黑", 9)).pack(side="left", padx=(5, 5))
        self.round_var = StringVar(value="1")
        self.round_display = Entry(control_bar, textvariable=self.round_var,
                                   state="readonly", readonlybackground="white",
                                   relief="sunken", width=6)
        self.round_display.pack(side="left", padx=(0, 10))

        auto_frame = Frame(control_bar)
        auto_frame.pack(side="right", padx=(5, 0))
        self.mode_whale = BooleanVar(value=False)
        self.mode_dwell = BooleanVar(value=False)
        if ENABLE_AUTO_MODE:
            # 实验性功能，默认关闭；SOS 的默认工作流是人工触发。
            Checkbutton(auto_frame, text="鲸吞模式", variable=self.mode_whale,
                        font=("Consolas", 12), fg="#000000", selectcolor="#f0f0f0",
                        command=lambda: self._on_mode_change("whale")).pack(side="left", padx=2)
            Checkbutton(auto_frame, text="栖鲸模式", variable=self.mode_dwell,
                        font=("Consolas", 12), fg="#000000", selectcolor="#f0f0f0",
                        command=lambda: self._on_mode_change("dwell")).pack(side="left", padx=2)

        input_frame = Frame(self)
        input_frame.pack(side="top", fill="both", expand=False, padx=5, pady=(5, 0))
        self.input_text = Text(input_frame, height=28, wrap="word")
        scroll_y = Scrollbar(input_frame, orient="vertical", command=self.input_text.yview)
        self.input_text.configure(yscrollcommand=scroll_y.set)
        self.input_text.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="left", fill="y")
        self.btn_send = Button(input_frame, text="发 送", width=8, height=2, command=self._on_send)
        self.btn_send.pack(side="right", padx=(5, 0))

        log_frame = Frame(self)
        log_frame.pack(side="top", fill="both", expand=True, padx=5, pady=(5, 0))
        Label(log_frame, text="AI模型聊天循环日志", font=("微软雅黑", 9, "bold")).pack(anchor="w")
        self.log_widget = scrolledtext.ScrolledText(log_frame, wrap="word", state="disabled")
        self.log_widget.pack(fill="both", expand=True)

        self.after(200, self._load_phase3_skills)
        self.after(300, self._load_freedom_tasks)
        self.after(500, self._refresh_freedom_tasks)

        self._auto_state = "idle"
        self._auto_target_round = 1

    def _on_mode_change(self, which):
        if which == "whale":
            if self.mode_whale.get():
                self.mode_dwell.set(False)
                self._auto_state = "idle"
                self.append_log("[鲸吞] 全自动模式已开启（自动启动+自动继续）")
            else:
                self.append_log("[鲸吞] 已关闭")
        else:
            if self.mode_dwell.get():
                self.mode_whale.set(False)
                self._auto_state = "idle"
                self.append_log("[栖鲸] 半自动模式已开启（仅自动启动）")
            else:
                self.append_log("[栖鲸] 已关闭")

    def _auto_start_task(self, retry_count=0):
        if not ENABLE_AUTO_MODE:
            return
        if not (self.mode_whale.get() or self.mode_dwell.get()):
            return
        if retry_count > 10:
            self.append_log("[自动] 重试次数耗尽，放弃自动启动")
            self._auto_state = "idle"
            return
        info = None
        if hasattr(self.app, 'phase3_manager'):
            info = self.app.phase3_manager.get_linked_task_info(self.task_name)
        if not info or not info["host"]:
            self.append_log("[自动] 无法启动：未获取到二级任务信息")
            self._auto_state = "idle"
            return
        panel = info["panel"]
        task_id = panel.task_id
        current_round = info["round"]
        if hasattr(self.app, 'phase2') and task_id in self.app.phase2.running_tasks:
            if self.app.phase2.running_tasks[task_id].get("is_running"):
                self.append_log(f"[自动] 上一轮任务仍在执行，等待1秒后重试... ({retry_count + 1}/10)")
                self.after(1000, lambda: self._auto_start_task(retry_count + 1))
                return
        self._auto_target_round = current_round
        self._auto_state = "waiting_round"
        self.append_log(f"[自动] 正在启动二级任务 [{info['name']}]，目标 {info['host']}，轮次 {current_round}")
        if hasattr(self.app, 'phase2'):
            self.app.phase2.run_task(task_id, panel, info["host"])

    def _auto_send_continue(self, retry_count=0):
        if not ENABLE_AUTO_MODE:
            return
        if not self.mode_whale.get():
            return
        # 检查死亡标记，若已死亡则直接放弃
        if hasattr(self.app, 'phase3_manager') and self.app.phase3_manager.is_dead(self.task_name):
            self._auto_state = "idle"
            return
        if retry_count > 30:
            if not (hasattr(self.app, 'phase3_manager') and self.app.phase3_manager.is_dead(self.task_name)):
                self.append_log("[鲸吞] 等待超时，放弃自动发送「继续」，请手动发送")
            self._auto_state = "idle"
            return
        if self.btn_send.cget('state') != 'normal':
            self.after(500, lambda: self._auto_send_continue(retry_count + 1))
            return
        self._auto_state = "waiting_deploy"
        self.append_log("[鲸吞] 自动发送「继续」，等待 AI 生成新计划...")
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", "继续")
        self._on_send()

    def _load_phase3_skills(self, event=None):
        if self.skill_combo['values']:
            return
        phase3_skills = []
        if self.app and hasattr(self.app, 'orchestrator') and self.app.orchestrator:
            for name, skill in self.app.orchestrator.skills.items():
                if "phase3" in skill.__class__.__module__:
                    phase3_skills.append(name)
        self.skill_combo['values'] = phase3_skills
        if phase3_skills:
            self.skill_combo.current(0)

    def _load_freedom_tasks(self, event=None):
        if self.task_combo['values']:
            return
        tasks = self.app.get_freedom_tasks_info() if hasattr(self.app, 'get_freedom_tasks_info') else []
        self.task_combo['values'] = [t['name'] for t in tasks]
        if tasks:
            self.task_combo.current(0)
            self._update_task_info(tasks[0])

    def _refresh_freedom_tasks(self):
        if not self.app or not hasattr(self.app, 'get_freedom_tasks_info'):
            return
        try:
            tasks = self.app.get_freedom_tasks_info()
            new_names = [t['name'] for t in tasks]
            current_names = list(self.task_combo['values'])
            if new_names != current_names:
                current_selected = self.task_var.get()
                self.task_combo['values'] = new_names
                if current_selected and current_selected in new_names:
                    self.task_var.set(current_selected)
                elif new_names:
                    self.task_combo.current(0)
                    self.task_var.set(new_names[0])
            selected_name = self.task_var.get()
            if selected_name:
                for t in tasks:
                    if t['name'] == selected_name:
                        new_round = t.get('round', 1)
                        self._update_task_info(t)
                        if self.mode_whale.get() and new_round > self._auto_target_round:
                            # 检查死亡标记，避免无效调用
                            if hasattr(self.app, 'phase3_manager') and self.app.phase3_manager.is_dead(self.task_name):
                                self._auto_state = "idle"
                                break
                            self.after(300, self._auto_send_continue)
                        break
                else:
                    if tasks:
                        self.task_combo.current(0)
                        self.task_var.set(tasks[0]['name'])
                        self._update_task_info(tasks[0])
        except Exception:
            pass
        self.after(500, self._refresh_freedom_tasks)

    def _on_task_selected(self, event=None):
        task_name = self.task_var.get()
        tasks = self.app.get_freedom_tasks_info()
        for t in tasks:
            if t['name'] == task_name:
                self._update_task_info(t)
                if hasattr(self.app, 'save_phase3_config'):
                    self.app.save_phase3_config(
                        self.task_name, linked_task=task_name, current_round=self.selected_round
                    )
                break

    def _update_task_info(self, task_info):
        self.selected_host = task_info.get('host', '')
        max_round = task_info.get('round', 1)
        self.round_var.set(str(max_round))
        self.selected_round = max_round

    def _on_send(self):
        skill_name = self.skill_var.get().strip()
        if not skill_name:
            self.append_log("[SYSTEM] 请先选择一个三级 Skill")
            return
        user_input = self.input_text.get("1.0", "end").strip()
        if not user_input:
            self.append_log("[SYSTEM] 请输入内容")
            return
        if user_input.strip() in ["杀死", "自杀", "终止"]:
            self.mode_whale.set(False)
            self.mode_dwell.set(False)
            self._auto_state = "idle"
            skill = self.app.orchestrator.skills.get(skill_name)
            if skill and hasattr(skill, 'kill_blocking_task'):
                chat_log = []
                if hasattr(self.app, 'phase3_manager'):
                    chat_log = self.app.phase3_manager.load_chat_log(self.task_name)
                result_data = json.dumps(chat_log, ensure_ascii=False)
                skill.kill_blocking_task(self.task_name, result_data)
                self.append_log("[DEATH] 已发送终止指令，套娃结束")
            else:
                self.append_log("[ERROR] 当前 Skill 不支持杀死指令")
            self.input_text.delete("1.0", "end")
            self.btn_send.config(state="normal")
            return
        try:
            self.selected_round = int(self.round_var.get())
        except Exception:
            self.selected_round = 1
        if hasattr(self.app, 'save_phase3_config'):
            self.app.save_phase3_config(self.task_name, skill_name=skill_name,
                                        linked_task=self.task_var.get(), current_round=self.selected_round)
        if (self.mode_whale.get() or self.mode_dwell.get()) and self._auto_state == "idle":
            self._auto_state = "waiting_deploy"
        self.input_text.delete("1.0", "end")
        self.btn_send.config(state="disabled")
        threading.Thread(target=self._run_skill, args=(skill_name, user_input), daemon=True).start()

    def _run_skill(self, skill_name, user_input):
        phase3_mgr = self.app.phase3_manager if hasattr(self.app, 'phase3_manager') else None
        task_name = self.task_name
        if not hasattr(self, '_session_started'):
            self._session_started = False
        if not self._session_started and phase3_mgr:
            try:
                phase3_mgr.start_session(task_name)
                self._session_started = True
                self.append_log("[LOG] 会话日志已清空")
            except Exception as e:
                self.append_log(f"[LOG ERROR] 清空日志失败: {e}")
        if phase3_mgr:
            try:
                phase3_mgr.append_chat_log(task_name, "user", user_input)
            except Exception as e:
                self.append_log(f"[LOG ERROR] 写入用户消息失败: {e}")
        try:
            skill = self.app.orchestrator.skills.get(skill_name)
            if not skill:
                self.append_log(f"[ERROR] Skill '{skill_name}' 未找到")
                return
            self.append_log(f"[INFO] 启动 Skill: {skill_name}")
            self.append_log(f"[HOST] {self.selected_host}")
            self.append_log(f"[ROUND] {self.selected_round}")
            self.append_log(f"[USER] {user_input}")
            result = skill.execute(user_input=user_input, app=self.app, log_fn=self.append_log,
                                   host=self.selected_host, round_num=self.selected_round,
                                   phase3_manager=phase3_mgr, task_name=task_name)
            if phase3_mgr and isinstance(result, str):
                try:
                    phase3_mgr.append_chat_log(task_name, "assistant", str(result)[:2000])
                except Exception:
                    pass
        except Exception as e:
            self.append_log(f"[ERROR] {str(e)}")
            if phase3_mgr:
                try:
                    phase3_mgr.append_chat_log(task_name, "assistant", f"[ERROR] {str(e)}")
                except Exception:
                    pass
        finally:
            if (self.mode_whale.get() or self.mode_dwell.get()) and self._auto_state == "waiting_deploy":
                self._auto_state = "waiting_start"
                self.after(500, self._auto_start_task)
            self.btn_send.config(state="normal")

    def append_log(self, msg):
        def _append():
            self.log_widget.config(state="normal")
            self.log_widget.insert("end", f"{msg}\n")
            self.log_widget.config(state="disabled")
            self.log_widget.see("end")
        if threading.current_thread() is threading.main_thread():
            _append()
        else:
            self.after(0, _append)
