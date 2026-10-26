"""ui/freedom_task_panel.py - 二级编排面板：步骤盒、参数、继承、轮次。"""

from tkinter import Frame, Label, Button, StringVar, scrolledtext, PanedWindow, ttk, Text, Canvas, Scrollbar
from tkinter import Toplevel, Listbox, MULTIPLE, END, messagebox, filedialog
import threading
import json
from pathlib import Path

class FreedomTaskPanel:
    def __init__(self, parent, task_name: str,
                 on_toggle_run=None,
                 on_stop_task=None,
                 on_kill_task=None,
                 host_list=None,
                 tool_list=None,
                 on_round_changed=None,
                 on_data_changed=None,
                 app=None):
        self.task_name = task_name
        self.current_round = 1
        self.max_round = 1
        self._toggle_callback = on_toggle_run
        self._stop_callback = on_stop_task
        self._kill_callback = on_kill_task
        self.on_round_changed = on_round_changed
        self.on_data_changed = on_data_changed  # 必须保存回调函数
        self.host_list = host_list or []
        self.tool_list = tool_list or []
        self.task_id = task_name
        self.tool_boxes = []
        self.is_running = False
        self.parent = parent
        self.app = app
        self._round_logs = {1: []}

        control_bar = Frame(parent)
        control_bar.pack(side="top", fill="x", padx=5, pady=2)

        self.state_var = StringVar(value="▶ 开始")
        self.btn_start = Button(control_bar, textvariable=self.state_var, width=8, command=self._on_toggle_click)
        self.btn_start.pack(side="left", padx=2)
        self.btn_stop = Button(control_bar, text="■ 停止", width=6, command=self._on_stop_click)
        self.btn_stop.pack(side="left", padx=2)
        self.btn_kill = Button(control_bar, text="★ 杀死", width=6, command=self._on_kill_click)
        self.btn_kill.pack(side="left", padx=2)

        right_container = Frame(control_bar)
        right_container.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.selected_host_var = StringVar()
        self.host_combo = ttk.Combobox(right_container, textvariable=self.selected_host_var, state="readonly", width=15)
        self.host_combo['values'] = self.host_list
        if self.host_list:
            self.host_combo.current(0)
        self.host_combo.pack(side="left", padx=(0, 5), fill="x", expand=True)

        # 轮次选择 Combobox
        Label(right_container, text="轮次:", font=("微软雅黑", 8)).pack(side="left", padx=(5, 0))
        self.round_var = StringVar(value="第1轮")
        self.round_combo = ttk.Combobox(right_container, textvariable=self.round_var,
                                        state="readonly", width=10)
        self.round_combo['values'] = ["第1轮"]
        self.round_combo.current(0)
        self.round_combo.pack(side="left", padx=2)
        self.round_combo.bind("<<ComboboxSelected>>", self._on_round_selected)

        self.btn_add = Button(right_container, text="＋添加", command=self._prompt_add_tool, width=6)
        self.btn_add.pack(side="right", padx=2)
        self.btn_remove = Button(right_container, text="－去除", command=self._remove_selected_boxes, width=6)
        self.btn_remove.pack(side="right", padx=2)

        self.pane = PanedWindow(parent, orient="vertical", sashrelief="raised", sashwidth=6, bg="#cccccc")
        self.pane.pack(fill="both", expand=True, padx=5, pady=(2, 0))

        self.free_layout_frame = Frame(self.pane, bg="#f8f8f8", relief="groove", bd=2)
        self.pane.add(self.free_layout_frame, minsize=20, height=700, stretch="always")
        Label(self.free_layout_frame, text="轮流程布局详情：", font=("微软雅黑", 9, "bold"), bg="#f8f8f8").pack(
            anchor="w", padx=5, pady=2)

        self._setup_scrollable_area()

        log_frame = Frame(self.pane)
        self.pane.add(log_frame, minsize=20, stretch="always")
        Label(log_frame, text="轮日志 (Round Log)", font=("微软雅黑", 9, "bold")).pack(anchor="w")
        self.log_widget = scrolledtext.ScrolledText(log_frame, wrap="word", state="disabled")
        self.log_widget.pack(fill="both", expand=True)

    def advance_round(self):
        self._save_current_log_to_cache()
        self.clear_log()
        self.current_round += 1
        self.max_round = self.current_round
        self._update_round_combo_values()
        self.round_var.set(f"第{self.current_round}轮")
        if self.current_round not in self._round_logs:
            self._round_logs[self.current_round] = []
        if self.on_round_changed:
            self.on_round_changed(self.current_round)
        if self.on_data_changed:
            self.on_data_changed()

    def _update_round_combo_values(self):
        values = [f"第{i}轮" for i in range(1, self.max_round + 1)]
        self.round_combo['values'] = values

    def _on_round_selected(self, event=None):
        selected = self.round_var.get()
        try:
            round_num = int(selected.replace("第", "").replace("轮", ""))
        except Exception:
            return
        if round_num == self.current_round:
            return
        self._save_current_log_to_cache()
        self.current_round = round_num
        self._load_log_from_cache(round_num)
        # 通知外部（但通常用户手动切换，标签也应更新）
        if self.on_round_changed:
            self.on_round_changed(round_num)

    def _save_current_log_to_cache(self):
        content = self.log_widget.get("1.0", END).strip()
        if content:
            self._round_logs[self.current_round] = content.splitlines()
        else:
            self._round_logs[self.current_round] = []

    def _load_log_from_cache(self, round_num):
        if round_num in self._round_logs and self._round_logs[round_num]:
            lines = self._round_logs[round_num]
        else:
            lines = self._load_round_log_from_disk(round_num)
            self._round_logs[round_num] = lines

        self.log_widget.config(state="normal")
        self.log_widget.delete("1.0", END)
        if not lines:
            self.log_widget.insert(END, f"[第{round_num}轮暂无日志记录]\n")
        else:
            for line in lines:
                self.log_widget.insert(END, line + "\n")
        self.log_widget.config(state="disabled")
        self.log_widget.see(END)

    def _load_round_log_from_disk(self, round_num):
        try:
            from config.tools import ROUND_BASE_DIR
            if not ROUND_BASE_DIR:
                return ["[ROUND_BASE_DIR 未配置]"]
            task_dir = ROUND_BASE_DIR / self.task_name
            summary_file = task_dir / f"{self.task_name}_round_{round_num}.json"
            if not summary_file.exists():
                return [f"[轮次文件不存在: {summary_file}]"]
            with open(summary_file, 'r', encoding='utf-8') as f:
                content = f.read()
            return content.splitlines()
        except Exception as e:
            return [f"[读取轮次日志失败: {e}]"]

    def set_round(self, round_num, notify=True):
        """设置当前轮次（用于恢复任务），不主动加载日志"""
        self._save_current_log_to_cache()
        self.current_round = round_num
        self.max_round = max(self.max_round, round_num)
        self._update_round_combo_values()
        self.round_var.set(f"第{round_num}轮")
        if round_num not in self._round_logs or not self._round_logs[round_num]:
            self._round_logs[round_num] = self._load_round_log_from_disk(round_num)
        if self.on_round_changed:
            self.on_round_changed(round_num)
        if notify and self.on_data_changed:
            self.on_data_changed()

    def append_log(self, msg):
        def _append():
            self.log_widget.config(state="normal")
            self.log_widget.insert(END, f"{msg}\n")
            self.log_widget.config(state="disabled")
            self.log_widget.see(END)
            if self.current_round not in self._round_logs:
                self._round_logs[self.current_round] = []
            self._round_logs[self.current_round].append(msg)
        if threading.current_thread() is threading.main_thread():
            _append()
        else:
            self.parent.after(0, _append)

    def clear_log(self):
        self.log_widget.config(state="normal")
        self.log_widget.delete(1.0, END)
        self.log_widget.config(state="disabled")
        self._round_logs[self.current_round] = []

    # UI 组件（不变）
    def _setup_scrollable_area(self):
        self.canvas = Canvas(self.free_layout_frame, bg="#f8f8f8", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar = Scrollbar(self.free_layout_frame, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.boxes_container = Frame(self.canvas, bg="#f8f8f8")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.boxes_container, anchor="nw")
        self.boxes_container.bind("<Configure>", self._on_container_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_container_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")

    def set_running(self, running: bool):
        self.is_running = running
        state = "disabled" if running else "normal"
        self.btn_start.config(state=state)
        self.btn_stop.config(state="normal" if running else "disabled")
        self.btn_kill.config(state="normal" if running else "disabled")
        self.btn_add.config(state=state)
        self.btn_remove.config(state=state)
        self.host_combo.config(state="disabled" if running else "readonly")
        self.round_combo.config(state="disabled" if running else "readonly")
        self.state_var.set("⏸ 运行中" if running else "▶ 开始")

    def _on_toggle_click(self):
        if self.is_running: return
        if self._toggle_callback:
            self._toggle_callback(self.task_id, self.state_var, self.get_selected_host())

    def _on_stop_click(self):
        if self._stop_callback:
            self._stop_callback(self.task_id, self.get_selected_host())

    def _on_kill_click(self):
        if self._kill_callback:
            self._kill_callback(self.task_id, self.get_selected_host())

    def get_selected_host(self):
        return self.selected_host_var.get().strip()

    def update_host_list(self, new_host_list):
        self.host_list = new_host_list
        current = self.selected_host_var.get()
        self.host_combo['values'] = new_host_list
        if current in new_host_list:
            self.host_combo.set(current)
        elif new_host_list:
            self.host_combo.current(0)

    def update_task_info(self, task_name=None, round_num=None):
        if task_name: self.task_name = task_name
        if round_num: self.set_round(round_num)

    def set_button_state(self, enabled):
        state = "normal" if enabled else "disabled"
        self.btn_start.config(state=state)
        self.btn_stop.config(state=state)
        self.btn_kill.config(state=state)
        self.host_combo.config(state="readonly" if enabled else "disabled")
        self.round_combo.config(state="readonly" if enabled else "disabled")

    def get_tool_boxes_data(self):
        tools = []
        for box in self.tool_boxes:
            param_str = box["param_text"].get(1.0, END).strip()
            inherit_from = box["inherit_combo"].get()
            inherit_file = box.get("inherit_file")
            tools.append({
                "name": box["name"],
                "type": box.get("type", "tool"),
                "params": param_str,
                "inherit_from": inherit_from if inherit_from != "无" and inherit_from is not None else None,
                "inherit_file": inherit_file
            })
        return {"tools": tools}

    def load_json_plan(self, plan_json):
        self._clear_all_boxes()
        tools = plan_json.get("tools", [])
        for tool in tools:
            self._add_tool_box(tool.get("name", ""), tool.get("params", '{"method":"GET","path":"/"}'),
                               tool.get("inherit_from", "无"), tool.get("type", "tool"))
        self._update_inherit_options()

    def _clear_all_boxes(self):
        for box in list(self.tool_boxes):
            box["frame"].destroy()
        self.tool_boxes.clear()
        self._update_inherit_options()

    def _prompt_add_tool(self, insert_after=None):
        if not self.tool_list:
            messagebox.showinfo("提示", "暂无可用工具")
            return
        popup = Toplevel()
        popup.title("选择工具/技能")
        popup.geometry("350x400")
        listbox = Listbox(popup, selectmode=MULTIPLE, exportselection=0)
        listbox.pack(fill="both", expand=1, padx=10, pady=10)
        for item in self.tool_list:
            if isinstance(item, dict):
                tag = "[SKILL]" if item.get("type") == "skill" else "[ATOMIC]"
                label = f"{item['name']} {tag}"
            else:
                label = str(item)
            listbox.insert(END, label)

        def confirm():
            selected_indices = listbox.curselection()
            if not selected_indices:
                messagebox.showwarning("提示", "请选择工具", parent=popup)
                return
            selected_items = [self.tool_list[i] for i in selected_indices]
            popup.destroy()
            for item in selected_items:
                if isinstance(item, dict):
                    name = item["name"]
                    tool_type = item.get("type", "tool")

                    # 判断是否为三级 Skill
                    is_phase3 = False
                    if tool_type == "skill" and self.app and hasattr(self.app, 'orchestrator'):
                        skill = self.app.orchestrator.skills.get(name)
                        if skill and "phase3" in skill.__class__.__module__:
                            is_phase3 = True

                    if is_phase3:
                        panel_name = self._ask_phase3_panel()
                        if panel_name:
                            params = json.dumps({"phase3_task": panel_name}, ensure_ascii=False)
                            self._add_tool_box(name, params=params, tool_type="skill", insert_after=insert_after)
                    else:
                        if tool_type == "skill":
                            self._add_tool_box(name, params='{"target":""}', tool_type="skill",
                                               insert_after=insert_after)
                        else:
                            self._add_tool_box(name, params='{"method":"GET","path":"/"}', tool_type="tool",
                                               insert_after=insert_after)
                else:
                    self._add_tool_box(item, insert_after=insert_after)

        Button(popup, text="确定添加", command=confirm).pack(pady=10)

    def _add_tool_box(self, tool_name, params=None, inherit_from="无", tool_type="tool", insert_after=None):
        combo_frame = Frame(self.boxes_container, bg="#f8f8f8")
        box_frame = Frame(combo_frame, bg="white", relief="raised", bd=2)
        box_frame.pack(side="top", fill="x", padx=10, pady=5)

        selected = False
        def toggle_select(e=None):
            nonlocal selected
            selected = not selected
            box_frame.config(highlightthickness=2, highlightbackground="#3399ff" if selected else "#ccc")
        box_frame.bind("<Button-1>", toggle_select)

        title = Frame(box_frame, bg="white")
        title.pack(fill="x", padx=3, pady=2)
        type_tag = f"[{tool_type.upper()}]" if tool_type else ""
        Label(title, text=f"{tool_name} {type_tag}", font=("微软雅黑", 8, "bold"), bg="white").pack(side="left")
        Button(title, text="×", width=2, command=lambda: self._remove_box(combo_frame)).pack(side="right")

        param_text = Text(box_frame, font=("Consolas", 9), wrap="word", height=6)
        param_text.pack(fill="x", padx=3, pady=2)
        param_text.insert(1.0, params if params else '{"method":"GET","path":"/"}')
        param_text.bind("<Button-1>", toggle_select)

        box_data = {
            "frame": combo_frame, "name": tool_name, "type": tool_type,
            "param_text": param_text, "inherit_combo": None, "inherit_file": None, "plus_frame": None
        }

        inherit_row = Frame(box_frame, bg="white")
        inherit_row.pack(fill="x", padx=3, pady=2)
        Label(inherit_row, text="继承:", font=("微软雅黑", 8), bg="white").pack(side="left")
        inherit_combo = ttk.Combobox(inherit_row, values=["无"], state="readonly", width=15)
        inherit_combo.pack(side="left", padx=(5, 0))
        inherit_combo.set(inherit_from)
        box_data["inherit_combo"] = inherit_combo

        btn_browse = Button(inherit_row, text="📂", width=2, height=1,
                            command=lambda b=box_data, c=inherit_combo: self._browse_inherit_file(b, c))
        btn_browse.pack(side="left", padx=(5, 0))

        plus_bar = Frame(combo_frame, bg="#f8f8f8")
        plus_bar.pack(side="top", anchor="center", pady=5)
        btn_plus = Button(plus_bar, text="+", width=2, command=lambda: self._prompt_add_tool(insert_after=combo_frame))
        btn_plus.pack(side="left")
        Label(plus_bar, text="↓", fg="green", font=("微软雅黑", 10, "bold"), bg="#f8f8f8").pack(side="left", padx=10)

        box_data["plus_frame"] = plus_bar
        self.tool_boxes.append(box_data)

        if insert_after and insert_after in [b["frame"] for b in self.tool_boxes]:
            combo_frame.pack(after=insert_after, side="top", fill="x", padx=10, pady=5)
        else:
            combo_frame.pack(side="top", fill="x", padx=10, pady=5)

        self._update_inherit_options()
        self.canvas.after(100, lambda: self.canvas.yview_moveto(1.0))

    def _browse_inherit_file(self, box_data, combo):
        from config.tools import ROUND_BASE_DIR
        initial_dir = str(ROUND_BASE_DIR) if ROUND_BASE_DIR.exists() else str(Path.home())
        file_path = filedialog.askopenfilename(
            title="选择继承的日志文件",
            initialdir=initial_dir,
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if not file_path: return
        box_data["inherit_file"] = file_path
        file_name = Path(file_path).name
        combo.set(f"📄 {file_name}")
        current_values = list(combo["values"])
        display_text = f"📄 {file_name}"
        if display_text not in current_values:
            combo["values"] = current_values + [display_text]

    def _remove_box(self, combo_frame):
        for box in list(self.tool_boxes):
            if box["frame"] == combo_frame:
                combo_frame.destroy()
                self.tool_boxes.remove(box)
                break
        self._update_inherit_options()

    def _remove_selected_boxes(self):
        for box in list(self.tool_boxes):
            inner = box["frame"].winfo_children()[0]
            if inner.cget("highlightbackground") == "#3399ff":
                box["frame"].destroy()
                self.tool_boxes.remove(box)
        self._update_inherit_options()

    def _ask_phase3_panel(self):
        """弹出窗口，选择要绑定的三级面板（排除自身）"""
        if not self.app or not hasattr(self.app, 'phase3_panels'):
            messagebox.showwarning("错误", "无法获取三级面板列表")
            return None

        # 获取所有三级面板名称，排除当前面板自己（如果当前面板被关联的话）
        all_panels = [p.task_name for p in self.app.phase3_panels.values()]
        # 排除当前 Freedom 任务自己（避免自循环）
        panels = [p for p in all_panels if p != self.task_name]

        if not panels:
            messagebox.showinfo("提示", "没有可用的其他三级面板，请先在决策台创建")
            return None

        win = Toplevel()
        win.title("选择三级面板（套娃）")
        win.geometry("350x160")
        win.transient(self.parent)
        win.grab_set()
        win.resizable(False, False)

        Label(win, text="请选择要绑定的三级面板名称：", font=("微软雅黑", 9)).pack(pady=(15, 5))
        combo = ttk.Combobox(win, values=panels, state="readonly", width=30)
        combo.pack(pady=5)
        combo.current(0)

        result = {"name": None}

        def on_ok():
            result["name"] = combo.get()
            win.destroy()

        def on_cancel():
            win.destroy()

        btn_frame = Frame(win)
        btn_frame.pack(pady=15)
        Button(btn_frame, text="确定", command=on_ok, width=10).pack(side="left", padx=10)
        Button(btn_frame, text="取消", command=on_cancel, width=10).pack(side="left", padx=10)

        win.wait_window()
        return result["name"]
    def _update_inherit_options(self):
        names = ["无"] + [b["name"] for b in self.tool_boxes]
        for box in self.tool_boxes:
            current_values = list(box["inherit_combo"]["values"])
            file_options = [v for v in current_values if v.startswith("📄")]
            box["inherit_combo"]["values"] = names + file_options