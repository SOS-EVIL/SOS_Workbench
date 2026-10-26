"""app.py - 主应用框架。"""
import sys, time, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.structured_logger import StructuredLogger
import json, threading
from datetime import datetime
from tkinter import *
from tkinter import ttk, messagebox, scrolledtext, simpledialog, filedialog

from core.deepseek_client import DeepSeekClient
from core.task_executor import TaskExecutor
from core.tool_registry import ToolRegistry
from core.shell_executor import ShellExecutor
from core.agent_orchestrator import AgentOrchestrator
from core.log_writer import LogWriter

from ui.custom_tab import CustomTabNotebook
from ui.main_panel import MainPanel
from ui.target_panel import TargetPanel
from ui.freedom_task_panel import FreedomTaskPanel

from utils.log_utils import clean_ansi, timestamp
from core import targets as target_api
from phases.phase1 import Phase1Collector
from phases.phase2 import Phase2Freedom
from phases.phase3 import Phase3Manager
from config.models import MODELS, MODEL_IDS, DEFAULT_MODEL, label_of as model_label
from config.version import __version__
from config.tools import (
    DEFAULT_CONCURRENCY, PROJECTS_DIR, ROUND_BASE_DIR, PHASE3_BASE_DIR,
    ENABLE_SHELL_ACTION, ENABLE_MODEL_ACTIONS, ensure_data_dirs,
)

class SOSWorkbenchApp:
    """主应用：UI 组装、目标管理、日志路由与模型动作注册。"""

    def _get_freedom_task_id_by_host(self, host):
        """根据 host 查找正在运行的编排台任务 ID"""
        if hasattr(self, 'phase2'):
            for task_id, info in self.phase2.running_tasks.items():
                if info.get("host") == host and info.get("is_running"):
                    return task_id
        return None

    def on_closing(self):
        self.stop_all_scans()
        try:
            self._log_writer.close()
        except Exception:
            pass
        if hasattr(self, 'structured_logger') and self.structured_logger:
            self.structured_logger.close()
        self.root.destroy()

    def add_manual_subpage_by_host(self, host):
        host = target_api.normalize_input(host) or host
        with self.targets_lock:
            if any((t.get("target") or t.get("host")) == host for t in self.targets):
                return f"目标 {host} 已存在"
            self.targets.append({
                "target": host, "host": host, "kind": target_api.kind_of(host),
                "ip": "", "is_alive": True, "source": "orchestrator"
            })
        self.root.after(0, lambda: self.create_target_tab(host))
        self.root.after(0, lambda: self.save_assets())
        self.root.after(0, lambda: self.start_all_btn.config(state=NORMAL))
        return f"目标 {host} 已创建"

    def start_scan_for_host(self, host):
        with self.targets_lock:
            need_create = host not in self.target_pause_events
        if need_create:
            self.add_manual_subpage_by_host(host)
            time.sleep(0.3)
        with self.targets_lock:
            btn = self.target_buttons.get(host)
        if btn is None:
            return f"目标 {host} 未正确初始化"
        self.toggle_target_scan(host, btn[1])
        return f"目标 {host} 任务已启动"

    def stop_scan_for_host(self, host):
        with self.targets_lock:
            if host in self.target_threads and self.target_threads[host].is_alive():
                self.stop_single_target(host)
                return f"目标 {host} 任务已停止"
        return f"目标 {host} 没有在运行"

    def run_single_tool_for_host(self, host, tool_name, params=None):
        if host not in self.target_pause_events:
            self.add_manual_subpage_by_host(host)
            time.sleep(0.3)
        all_tools = self.tool_registry.get_all_tools()
        if tool_name not in all_tools:
            available = ', '.join(all_tools.keys())
            return f"工具 {tool_name} 不可用，可用工具：{available}"
        with self.targets_lock:
            pause_evt = self.target_pause_events.get(host)
        if pause_evt is None:
            return f"目标 {host} 未正确初始化"

        # 如果该 host 已有任务在运行，拒绝重复启动
        with self.targets_lock:
            if host in self.target_threads and self.target_threads[host].is_alive():
                return f"目标 {host} 已有任务在运行，请等待完成或先杀死"

        # 确保暂停事件处于"未暂停"状态（杀死后可能残留为 set 状态）
        pause_evt.clear()

        def target():
            with self.targets_lock:
                self.target_threads[host] = threading.current_thread()
            try:
                self.phase1.run_single_tool(
                    host=host, project_dir=self.project_dir, pause_event=pause_evt,
                    tool_name=tool_name, log_target=self.log_target,
                    log_target_program=self.log_target_program, store_tool_output=self.store_tool_output,
                    params=params
                )
            finally:
                with self.targets_lock:
                    if self.target_threads.get(host) is threading.current_thread():
                        self.target_threads.pop(host, None)
            self.root.after(0, lambda: self.analyze_btn.config(state=NORMAL))

        th = threading.Thread(target=target, daemon=True)
        th.start()
        return f"目标 {host} 的 {tool_name} 任务已启动"

    def shell_exec_command(self, command):
        result = ShellExecutor.run(command)
        self.log_global(f"[shell] {command[:60]}... 执行完毕", sensitive=True)
        return result

    def get_collected_data_summary(self, host=None):
        with self.targets_lock:
            if host:
                data = self.collected_data.get(host, {})
                return {host: {t: f"{len(l)} lines" for t, l in data.items()}}
            else:
                return {h: {t: f"{len(l)} lines" for t, l in data.items()} for h, data in self.collected_data.items()}

    def list_subpages(self):
        with self.targets_lock:
            hosts = [t.get("target") or t.get("host") for t in self.targets
                     if (t.get("target") or t.get("host"))]
        return hosts

    def _register_orchestrator_actions(self):
        if not ENABLE_MODEL_ACTIONS:
            # 默认关闭：模型只给建议，执行由人触发。
            return
        if ENABLE_SHELL_ACTION:
            # 默认关闭：把任意命令执行交给模型等于通用 RCE 分发器。
            self.orchestrator.register_action("shell_exec",
                lambda command=None, cmd=None, action=None: self.shell_exec_command(command or cmd),
                "执行临时 shell 命令并返回结果。")
        self.orchestrator.register_action("run_tool",
            lambda host=None, tool=None, tool_name=None: self.run_single_tool_for_host(host, tool or tool_name),
            "对指定目标执行一个工具，参数示例 {\"host\": \"目标\", \"tool\": \"工具名\"}")
        self.orchestrator.register_action("start_target", lambda host: self.start_scan_for_host(host), "启动目标的执行")
        self.orchestrator.register_action("stop_target", lambda host: self.stop_scan_for_host(host), "暂停目标的执行")
        self.orchestrator.register_action("run_global_analysis", lambda: self.run_global_analysis(), "汇总分析")
        self.orchestrator.register_action(
            "get_collected_data",
            lambda host=None: self.get_collected_data_summary(host),
            "获取已收集结果摘要",
        )
        self.orchestrator.register_action("list_targets", lambda: self.list_subpages(), "列出当前项目的所有目标")

    def __init__(self, root, key_mgr):
        self.root = root
        self.root.title(f"SOS_Workbench {__version__}")
        self.root.geometry("1400x900")
        self.root.minsize(1100, 700)
        ensure_data_dirs()
        self.key_mgr = key_mgr
        self.stored_key = key_mgr.load()
        self._load_model_config()
        self.chat_client = DeepSeekClient(self.stored_key, model=self.current_model)
        self.project_dir = None
        self.project_name = ""
        self.targets = []
        self.target_logs = {}
        self.target_program_logs = {}
        self.target_buttons = {}
        self.target_pause_events = {}
        self.target_threads = {}
        self.collected_data = {}
        self.recent_log = []
        self.concurrency = IntVar(value=DEFAULT_CONCURRENCY)
        self.targets_lock = threading.RLock()
        self._log_lock = threading.Lock()          # 仅保护 recent_log
        self._log_writer = LogWriter(lambda: self.project_dir)
        self.freedom_panels = {}
        self.freedom_logs = {}
        self._freedom_tasks_loaded = False
        self._loaded_freedom_task_names = set()

        self.build_ui()

        self.executor = TaskExecutor(
            concurrency_getter=lambda: self.concurrency.get(),
            tool_resolver=lambda: self.tool_registry.get_all_tools(),
        )
        self.tool_registry = ToolRegistry()
        self.phase1 = Phase1Collector(self, executor=self.executor)
        self.phase2 = Phase2Freedom(self)
        self.phase3_manager = Phase3Manager(self)
        self.orchestrator = AgentOrchestrator(
            self.chat_client,
            on_message=lambda sender, text: self.root.after(0, lambda: self.display_chat(sender, text)),
            app=self
        )
        self._register_orchestrator_actions()
        self.structured_logger = StructuredLogger.get_instance()
        self.load_projects_on_start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def build_ui(self):
        main_pane = PanedWindow(self.root, orient=HORIZONTAL, sashrelief=RAISED, sashwidth=4)
        main_pane.pack(fill=BOTH, expand=True, padx=10, pady=10)

        left_frame = Frame(main_pane, width=350)
        main_pane.add(left_frame)

        project_frame = Frame(left_frame, relief=GROOVE, bd=2)
        project_frame.pack(fill=BOTH, expand=False, pady=(0, 5))
        Label(project_frame, text="项目管理", font=("微软雅黑", 10, "bold")).pack(anchor=W, padx=5, pady=5)

        row_proj = Frame(project_frame)
        row_proj.pack(fill=X, padx=5, pady=2)
        self.project_combo = ttk.Combobox(row_proj, state="readonly", width=18)
        self.project_combo.pack(side=LEFT, expand=True, fill=X)
        self.project_combo.bind("<<ComboboxSelected>>", self.on_project_selected)
        Button(row_proj, text="打开", command=self.open_project, width=4).pack(side=LEFT, padx=(5, 2))
        Button(row_proj, text="新建", command=self.new_project, width=4).pack(side=LEFT, padx=2)

        api_row = Frame(project_frame)
        api_row.pack(fill=X, padx=5, pady=2)
        api_row.pack_propagate(False)
        api_row.config(height=28)
        Label(api_row, text="API Key:").pack(side=LEFT, padx=(0, 5))
        self.api_entry = Entry(api_row, show="*", width=15)
        self.api_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))
        if self.stored_key:
            self.api_entry.insert(0, self.key_mgr.mask_key(self.stored_key))
        self.api_entry.bind("<FocusIn>", lambda e: self.api_entry.selection_range(0, END))
        Button(api_row, text="保存", command=self.save_api_key, width=6).pack(side=LEFT)

        model_row = Frame(project_frame)
        model_row.pack(fill=X, padx=5, pady=(0, 3))
        Label(model_row, text="模型:", font=("微软雅黑", 9)).pack(side=LEFT, padx=(0, 4))
        self.current_model_var = StringVar(value=self.current_model)
        self.model_radios = {}
        for spec in MODELS:
            rb = Radiobutton(
                model_row, text=spec["label"], variable=self.current_model_var,
                value=spec["id"], command=self._on_model_change,
                indicatoron=True, padx=0, pady=0, font=("微软雅黑", 8),
            )
            rb.pack(side=LEFT, padx=(0, 6))
            self.model_radios[spec["id"]] = rb

        chat_frame = Frame(left_frame, relief=GROOVE, bd=2)
        chat_frame.pack(fill=BOTH, expand=True)
        self.chat_title = Label(chat_frame, text=f"{model_label(self.current_model)} 助手",
                                font=("微软雅黑", 10, "bold"))
        self.chat_title.pack(anchor=W, padx=5, pady=5)
        self.chat_log = scrolledtext.ScrolledText(chat_frame, height=12, state=DISABLED, wrap=WORD)
        self.chat_log.pack(fill=BOTH, expand=True, padx=5, pady=(0, 5))
        input_outer = Frame(chat_frame)
        input_outer.pack(fill=X, padx=5, pady=(0, 5))
        self.chat_input = Text(input_outer, height=6, wrap=WORD)
        chat_scroll = Scrollbar(input_outer, orient=VERTICAL, command=self.chat_input.yview)
        self.chat_input.configure(yscrollcommand=chat_scroll.set)
        self.chat_input.pack(side=LEFT, fill=BOTH, expand=True)
        chat_scroll.pack(side=LEFT, fill=Y)
        self.chat_input.bind("<Control-Return>", self.send_chat)
        Button(input_outer, text="发 送", command=self.send_chat, width=10).pack(side=RIGHT, padx=5)

        right_frame = Frame(main_pane)
        main_pane.add(right_frame)
        self.phase_notebook = ttk.Notebook(right_frame)
        self.phase_notebook.pack(fill=BOTH, expand=True)

        phase1 = Frame(self.phase_notebook)
        self.phase_notebook.add(phase1, text="执行台")
        toolbar = Frame(phase1, height=30)
        toolbar.pack(side=TOP, fill=X, padx=5, pady=5)
        toolbar.pack_propagate(False)
        Label(toolbar, text="并发数:").pack(side=LEFT)
        Spinbox(toolbar, from_=1, to=10, width=4, textvariable=self.concurrency).pack(side=LEFT, padx=5)
        self.start_all_btn = Button(toolbar, text="▶ 全部开始", command=self.start_all_scans, state=DISABLED)
        self.start_all_btn.pack(side=LEFT, padx=5)
        self.stop_all_btn = Button(toolbar, text="■ 全部停止", command=self.stop_all_scans, state=DISABLED)
        self.stop_all_btn.pack(side=LEFT)
        self.target_notebook = CustomTabNotebook(phase1)
        self.target_notebook.pack(fill=BOTH, expand=True, padx=5, pady=(5, 0))
        self.target_notebook.set_add_callback(self.add_manual_subpage)
        self.target_notebook.set_remove_callback(self.remove_subpage)
        self.main_panel = MainPanel(self.target_notebook, analyze_callback=self.run_global_analysis)
        self.target_notebook.add(self.main_panel, text="主界面")
        self.global_log = self.main_panel.global_log
        self.summary_log = self.main_panel.summary_log
        self.analyze_btn = self.main_panel.analyze_btn

        phase2 = Frame(self.phase_notebook)
        self.phase_notebook.add(phase2, text="编排台")
        self.freedom_notebook = CustomTabNotebook(phase2)
        self.freedom_notebook.pack(fill=BOTH, expand=True, padx=5, pady=(0, 0))
        self.freedom_notebook.set_add_callback(self.add_freedom_task)
        self.freedom_notebook.set_remove_callback(self.remove_freedom_task)

        phase3 = Frame(self.phase_notebook)
        self.phase_notebook.add(phase3, text="决策台")
        self.phase3_notebook = CustomTabNotebook(phase3)
        self.phase3_notebook.pack(fill="both", expand=True, padx=5, pady=(5, 0))
        self.phase3_notebook.set_add_callback(self._add_phase3_task)
        self.phase3_notebook.set_remove_callback(self._remove_phase3_task)
        # 存储决策台任务面板 {task_id: panel}
        self.phase3_panels = {}


    def create_target_tab(self, host):
        tab_frame = Frame(self.target_notebook)
        panel = TargetPanel(parent=tab_frame, host=host,
                            on_toggle_scan=self.toggle_target_scan,
                            on_stop_scan=self.stop_single_target,
                            on_kill_scan=self.kill_single_target)
        with self.targets_lock:
            self.target_logs[host] = panel.log_widget
            self.target_program_logs[host] = panel.prog_widget
            self.target_buttons[host] = (panel, panel.state_var)
            self.target_pause_events[host] = threading.Event()
        self.target_notebook.add(tab_frame, text=target_api.alias(host))
        tab_frame.host = host

    def add_manual_subpage(self):
        url = simpledialog.askstring(
            "添加目标",
            "输入目标（可以是任何东西，平台不解释它的含义）：\n\n"
            "  域名          example.com\n"
            "  主机:端口      10.0.0.5:8080\n"
            "  数据库        db:postgresql://user@host:5432/db\n"
            "  本地文件      D:\\data\\2026\\app.db\n"
            "  带类型前缀     file:D:\\data\\2026\\app.db",
            parent=self.root,
        )
        if not url: return
        # target 归一化：普通域名/URL 走旧规则；db:/file:/DSN/本地路径原样保留
        host = target_api.normalize_input(url)
        if not host: return
        with self.targets_lock:
            if any((t.get("target") or t.get("host")) == host for t in self.targets):
                messagebox.showwarning("重复", f"目标 {host} 已存在")
                return
            self.targets.append({
                "target": host, "host": host, "kind": target_api.kind_of(host),
                "ip": "", "is_alive": True, "source": "manual"
            })
        self.create_target_tab(host)
        self.save_assets()
        self.start_all_btn.config(state=NORMAL)
        self.log_global(f"已添加目标: {host}", host=host)
        self._update_all_freedom_hosts()

    def remove_subpage(self, index):
        tabs = self.target_notebook.tabs()
        if 0 <= index < len(tabs):
            frame = tabs[index]
            host_to_remove = getattr(frame, 'host', None)
            if host_to_remove and host_to_remove in self.target_logs:
                with self.targets_lock:
                    self.targets = [t for t in self.targets if t.get("host") != host_to_remove]
                    self.target_logs.pop(host_to_remove, None)
                    self.target_program_logs.pop(host_to_remove, None)
                    self.target_buttons.pop(host_to_remove, None)
                    self.target_pause_events.pop(host_to_remove, None)
                    self.target_threads.pop(host_to_remove, None)
                    self.collected_data.pop(host_to_remove, None)
                self.target_notebook.forget(index)
                self.save_assets()
                self.log_global(f"已删除目标: {host_to_remove}", sensitive=True)
                self._update_all_freedom_hosts()
                with self.targets_lock:
                    if not self.targets:
                        self.start_all_btn.config(state=DISABLED)

    def save_assets(self):
        if not self.project_dir: return
        with self.targets_lock:
            targets_copy = list(self.targets)
            total = len(targets_copy)
        assets_file = self.project_dir / "assets.json"
        normalized = []
        for t in targets_copy:
            tgt = t.get("target") or t.get("host") or t.get("ip") or ""
            if not tgt:
                continue
            item = dict(t)
            item["target"] = tgt
            item["host"] = tgt                      # 兼容旧字段（旧数据/阶段3读取）
            item["kind"] = target_api.kind_of(tgt)
            normalized.append(item)
        data = {"domain": self.project_name, "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "total_assets": total, "targets": normalized}
        with open(assets_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def load_projects_on_start(self):
        self.refresh_project_list()
        if self.project_combo['values']:
            self.project_combo.current(0)
            self.on_project_selected()
        else:
            self.log_global("暂无项目，请新建或打开")
        self._load_freedom_tasks()
        self._load_phase3_tasks()

    def refresh_project_list(self):
        valid_projects = []
        try:
            for d in PROJECTS_DIR.iterdir():
                if d.is_dir() and (d / "assets.json").exists():
                    valid_projects.append(d.name)
        except FileNotFoundError:
            PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.project_combo['values'] = valid_projects

    def on_project_selected(self, event=None):
        name = self.project_combo.get()
        if not name: return
        self.project_dir = PROJECTS_DIR / name
        self.project_name = name
        with self.targets_lock:
            self.collected_data.clear()
        self.load_targets()
        self.start_all_btn.config(state=NORMAL if self.targets else DISABLED)
        self.analyze_btn.config(state=DISABLED)
        self.log_global(f"已载入项目：{name}，共 {len(self.targets)} 个目标")
        self.structured_logger.set_project_dir(self.project_dir)

    def open_project(self):
        dir_path = filedialog.askdirectory(initialdir=PROJECTS_DIR, title="选择项目目录")
        if not dir_path: return
        self.project_dir = Path(dir_path)
        self.project_name = self.project_dir.name
        assets_file = self.project_dir / "assets.json"
        if not assets_file.exists():
            go_on = messagebox.askyesno("未找到资产文件", f"项目 {self.project_name} 中没有 assets.json。\n\n是否手动输入主标识以继续？")
            if go_on:
                target = simpledialog.askstring("主标识", f"请输入项目 {self.project_name} 的主标识：")
                if target:
                    target = target.strip()
                    with self.targets_lock:
                        self.targets = [{
                            "target": target, "host": target,
                            "kind": target_api.kind_of(target), "ip": "", "is_alive": True,
                        }]
                        self.collected_data.clear()
                    self.load_targets()
                    self.start_all_btn.config(state=NORMAL)
                    self.refresh_project_list()
                    self.project_combo.set(self.project_name)
                    self.log_global(f"已打开项目：{self.project_name}（手动设置主标识）")
                    self.structured_logger.set_project_dir(self.project_dir)
                    return
            with self.targets_lock:
                self.targets = []
                self.collected_data.clear()
            self.load_targets()
            self.start_all_btn.config(state=DISABLED)
            self.refresh_project_list()
            self.project_combo.set(self.project_name)
            self.log_global(f"已打开项目：{self.project_name}（无资产文件）")
            self.structured_logger.set_project_dir(self.project_dir)
            return
        with self.targets_lock:
            self.collected_data.clear()
        self.load_targets()
        self.start_all_btn.config(state=NORMAL if self.targets else DISABLED)
        self.analyze_btn.config(state=DISABLED)
        self.refresh_project_list()
        self.project_combo.set(self.project_name)
        self.log_global(f"已打开项目：{self.project_name}，共 {len(self.targets)} 个目标")
        self.structured_logger.set_project_dir(self.project_dir)

    def new_project(self):
        self.show_project_settings()

    def show_project_settings(self):
        win = Toplevel(self.root)
        win.title("项目设置")
        win.geometry("450x300")
        win.transient(self.root)
        win.grab_set()
        Label(win, text="项目名称").pack(pady=(10, 0))
        name_entry = Entry(win, width=50)
        name_entry.pack()
        name_entry.insert(0, self.project_name or "")
        Label(win, text="主标识（项目内的任意 target，例如 example.com）").pack(pady=(10, 0))
        target_entry = Entry(win, width=50)
        target_entry.pack()

        def on_confirm():
            name = name_entry.get().strip()
            target = target_entry.get().strip()
            if not name or not target:
                messagebox.showwarning("提示", "项目名称和主标识不能为空")
                return
            new_dir = PROJECTS_DIR / name
            if new_dir.exists() and str(new_dir) != str(self.project_dir):
                messagebox.showwarning("提示", "项目名称已存在")
                return
            new_dir.mkdir(parents=True, exist_ok=True)
            self.project_dir = new_dir
            self.project_name = name
            with self.targets_lock:
                self.collected_data.clear()
            self.stop_all_scans()
            while len(self.target_notebook.tabs()) > 1:
                self.target_notebook.forget(1)
            with self.targets_lock:
                self.target_logs.clear()
                self.target_program_logs.clear()
                self.target_buttons.clear()
                self.target_pause_events.clear()
                self.target_threads.clear()
            win.destroy()
            self.log_global(f"项目 {name} 创建成功")
            self.project_combo.unbind("<<ComboboxSelected>>")
            self.project_combo.bind("<<ComboboxSelected>>", self.on_project_selected)

            with self.targets_lock:
                self.targets = [{
                    "target": target, "host": target, "kind": target_api.kind_of(target),
                    "ip": "", "is_alive": True, "source": "manual",
                }]
            self.save_assets()
            self.refresh_project_list()
            self.project_combo.set(name)
            self.load_targets()
            self.start_all_btn.config(state=NORMAL)
            self.analyze_btn.config(state=DISABLED)
            self.structured_logger.set_project_dir(self.project_dir)
            self.log_global(f"项目 {name} 初始化完成，共 1 个目标")

        Button(win, text="确定", command=on_confirm).pack(pady=20)

    def load_targets(self):
        with self.targets_lock:
            self.target_logs.clear()
            self.target_program_logs.clear()
            self.target_buttons.clear()
            self.target_pause_events.clear()
            self.target_threads.clear()
        while len(self.target_notebook.tabs()) > 1:
            self.target_notebook.forget(1)
        if self.project_dir:
            assets_file = self.project_dir / "assets.json"
            if assets_file.exists():
                with open(assets_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                targets = data.get("targets", [])
            else:
                targets = []
        else:
            targets = []
        with self.targets_lock:
            self.targets = targets
        for t in self.targets:
            host = t.get("target") or t.get("host") or t.get("ip")
            if not host: continue
            self.create_target_tab(host)

    def toggle_target_scan(self, host, state_var):
        # 先检查是否有编排台任务在运行
        task_id = self._get_freedom_task_id_by_host(host)
        if task_id:
            # 存在二级任务，直接操作其 pause_event
            pe = self.target_pause_events.get(host)
            if not pe:
                # 异常：从 phase2 中取
                info = self.phase2.running_tasks.get(task_id)
                pe = info.get("pause_event") if info else None
            if pe:
                if pe.is_set():
                    pe.clear()
                    state_var.set("⏸ 暂停")
                    self.log_global(f"{host} 继续执行", host=host)
                else:
                    pe.set()
                    state_var.set("▶ 继续")
                    self.log_global(f"{host} 已暂停", host=host)
            return

        with self.targets_lock:
            pause_evt = self.target_pause_events.get(host)
            if pause_evt is None:
                state_var.set("▶ 开始")
                self.log_global(f"{host} 请使用编排台「编排台」启动任务", host=host)
                return
            if host in self.target_threads:
                if pause_evt.is_set():
                    pause_evt.clear()
                    state_var.set("⏸ 暂停")
                    self.log_global(f"{host} 继续执行", host=host)
                else:
                    pause_evt.set()
                    state_var.set("▶ 继续")
                    self.log_global(f"{host} 已暂停", host=host)
            else:
                state_var.set("▶ 开始")
                self.log_global(f"{host} 无运行中的任务", host=host)

    def stop_single_target(self, host):
        with self.targets_lock:
            pause_evt = self.target_pause_events.get(host)
            # 只要存在暂停事件，就发送暂停信号（无论是否注册了线程）
            if pause_evt:
                pause_evt.set()
            if host in self.target_buttons:
                panel, state_var = self.target_buttons[host]
                state_var.set("▶ 开始")
            self.log_global(f"{host} 已暂停/停止", host=host)

    def kill_single_target(self, host):
        task_id = self._get_freedom_task_id_by_host(host)
        if task_id:
            self.phase2.kill_task(task_id)
            self.log_global(f"[强制杀死] {host} 编排台任务已终止", host=host)
            if host in self.target_buttons:
                panel, state_var = self.target_buttons[host]
                state_var.set("▶ 开始")
            with self.targets_lock:
                self.target_threads.pop(host, None)
                if host in self.target_pause_events:
                    self.target_pause_events[host].set()
            return

        with self.targets_lock:
            pause_evt = self.target_pause_events.get(host)
            if pause_evt:
                pause_evt.set()
            self.target_threads.pop(host, None)
            if host in self.target_buttons:
                panel, state_var = self.target_buttons[host]
                state_var.set("▶ 开始")
        self.phase1.kill_execution(host)
        self.log_global(f"[强制杀死] {host} 所有进程已终止，环境已清理", host=host)

    def register_freedom_thread(self, host, pause_event):
        """编排台开始任务时调用，注册暂停事件和虚拟线程"""
        with self.targets_lock:
            # 保存编排台传过来的暂停事件
            self.target_pause_events[host] = pause_event
            # 创建一个已完成的虚拟线程，仅用于标记“有任务在运行”
            dummy = threading.Thread(target=lambda: None)
            dummy.start()          # 立即结束，is_alive() 为 False，但对象存在
            self.target_threads[host] = dummy

    def unregister_freedom_thread(self, host):
        """编排台任务结束/停止/杀死时调用，清理注册信息"""
        with self.targets_lock:
            self.target_threads.pop(host, None)
            # 保留 target_pause_events 中的事件对象，以便下次复用

    def start_all_scans(self):
        self.log_global("批量启动已禁用，请在「编排台」页签中编排并启动任务")

    def stop_all_scans(self):
        with self.targets_lock:
            hosts = list(self.target_threads.keys())
            for host in hosts:
                pause_evt = self.target_pause_events.get(host)
                if pause_evt: pause_evt.set()
                if host in self.target_buttons:
                    panel, state_var = self.target_buttons[host]
                    state_var.set("▶ 开始")
        self.stop_all_btn.config(state=DISABLED)
        self.start_all_btn.config(state=NORMAL)
        self.log_global("已发出暂停所有任务指令")

    # 编排台方法

    def _get_structured_tool_list(self):
        """生成带类型标记的工具列表 [{name, type}, ...]"""
        items = []
        for name in self.tool_registry.get_all_tools().keys():
            items.append({"name": name, "type": "tool"})
        if self.orchestrator:
            for name in self.orchestrator.skills.keys():
                items.append({"name": name, "type": "skill"})
        return items

    def add_freedom_task(self, task_name=None):
        if not task_name:
            task_name = simpledialog.askstring("新增编排任务", "请输入任务名称:", parent=self.root)
        if not task_name or not task_name.strip(): return
        task_name = task_name.strip()

        try:
            task_round_dir = ROUND_BASE_DIR / task_name
            task_round_dir.mkdir(parents=True, exist_ok=True)
            plan_file = task_round_dir / "plan.json"
            with open(plan_file, "w", encoding="utf-8") as f:
                json.dump({"tools": []}, f, ensure_ascii=False, indent=2)
            self.log_global(f"[编排] 任务已创建：{task_round_dir}")
        except Exception as e:
            messagebox.showerror("错误", f"创建任务失败：{str(e)}", parent=self.root)
            return

        task_id = f"{task_name}_{int(time.time())}"
        tab_frame = Frame(self.freedom_notebook)
        current_hosts = self.list_subpages()

        def update_tab_label(round_num):
            for t in self.freedom_notebook._tabs:
                if t['frame'] is tab_frame:
                    t['btn'].config(text=f"{task_name} · 第{round_num}轮")
                    break

        panel = FreedomTaskPanel(
            parent=tab_frame, task_name=task_name,
            on_toggle_run=self._on_freedom_toggle,
            on_stop_task=self._on_freedom_stop,
            on_kill_task=self._on_freedom_kill,
            host_list=current_hosts,
            tool_list=self._get_structured_tool_list(),
            on_round_changed=update_tab_label,
            on_data_changed=self._refresh_all_phase3_panels,
            app=self
        )
        panel.task_id = task_id
        self.freedom_panels[task_id] = panel
        self.freedom_logs[task_id] = panel.log_widget
        self.freedom_notebook.add(tab_frame, text=f"{task_name} · 第1轮")
        tab_frame.panel = panel
        self.log_global(f"[编排] 任务面板加载完成：{task_name}")
        self._save_freedom_task(task_name, panel)

    def _on_freedom_toggle(self, task_id, state_var, selected_host):
        if not selected_host:
            self.log_global(f"[编排] 任务 {task_id} 失败：请先选择目标目标")
            return
        panel = self.freedom_panels.get(task_id)
        if not panel: return
        state_var.set("⏸ 暂停")
        self.phase2.run_task(task_id, panel, selected_host)
        def reset_button():
            while self.phase2.running_tasks.get(task_id, {}).get("is_running", False):
                time.sleep(0.5)
            state_var.set("▶ 开始")
        threading.Thread(target=reset_button, daemon=True).start()

    def _on_freedom_stop(self, task_id, selected_host):
        self.phase2.stop_task(task_id)
        self.log_global(f"[编排] 任务 {task_id} 已停止，目标：{selected_host}")

    def _on_freedom_kill(self, task_id, selected_host):
        self.phase2.kill_task(task_id)
        self.log_global(f"[编排] 任务 {task_id} 已强制终止，目标：{selected_host}")

    def _update_all_freedom_hosts(self):
        current_hosts = self.list_subpages()
        if isinstance(current_hosts, str): current_hosts = []
        for panel in self.freedom_panels.values():
            self.root.after(0, lambda p=panel: p.update_host_list(current_hosts))

    def _save_freedom_task(self, task_name, panel):
        task_dir = ROUND_BASE_DIR / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        config_file = task_dir / "task_config.json"
        data = {"task_name": task_name, "current_round": panel.current_round, "plan": panel.get_tool_boxes_data()}
        try:
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log_global(f"[编排] 保存配置失败：{str(e)}")

    def _load_freedom_tasks(self):
        # 双重保险：标记已加载过，直接返回
        if self._freedom_tasks_loaded:
            return
        self._freedom_tasks_loaded = True
        if not ROUND_BASE_DIR.exists():
            return
        for task_folder in ROUND_BASE_DIR.iterdir():
            if task_folder.is_dir():
                task_name = task_folder.name
                try:
                    plan_file = task_folder / "plan.json"
                    plan = {}
                    if plan_file.exists():
                        with open(plan_file, "r", encoding="utf-8") as f:
                            plan = json.load(f)
                    self._add_freedom_task_from_data(task_name, 1, plan)
                except Exception as e:
                    self.log_global(f"[编排] 加载任务失败 {task_name}：{e}")

    def _add_freedom_task_from_data(self, task_name, current_round, plan):
        # 如果已经恢复过这个任务，直接跳过
        if task_name in self._loaded_freedom_task_names:
            return
        self._loaded_freedom_task_names.add(task_name)
        # 面板去重（保留）
        if any(p.task_name == task_name for p in self.freedom_panels.values()):
            return
        # ... 原有代码不变 ...
        current_hosts = self.list_subpages()
        task_id = f"{task_name}_{int(time.time())}"
        tab_frame = Frame(self.freedom_notebook)

        def update_tab_label(round_num):
            for t in self.freedom_notebook._tabs:
                if t['frame'] is tab_frame:
                    t['btn'].config(text=f"{task_name} · 第{round_num}轮")
                    break

        panel = FreedomTaskPanel(
            parent=tab_frame, task_name=task_name,
            on_toggle_run=self._on_freedom_toggle,
            on_stop_task=self._on_freedom_stop,
            on_kill_task=self._on_freedom_kill,
            host_list=current_hosts,
            tool_list=self._get_structured_tool_list(),
            on_round_changed=update_tab_label,
            on_data_changed=self._refresh_all_phase3_panels,
            app=self
        )
        panel.task_id = task_id
        panel.current_round = current_round
        if plan.get("tools"):
            panel.load_json_plan(plan)
        panel.set_round(current_round, notify=False)
        self.freedom_panels[task_id] = panel
        self.freedom_logs[task_id] = panel.log_widget
        self.freedom_notebook.add(tab_frame, text=f"{task_name} · 第{current_round}轮")
        tab_frame.panel = panel
        self.log_global(f"[编排] 已恢复任务：{task_name}")

    def remove_freedom_task(self, index):
        tabs = self.freedom_notebook.tabs()
        if 0 <= index < len(tabs):
            tab_frame = tabs[index]
            panel = getattr(tab_frame, 'panel', None)
            if not panel: return
            task_id = panel.task_id
            task_name = panel.task_name
            if hasattr(self.phase2, 'running_tasks') and task_id in self.phase2.running_tasks:
                self.phase2.kill_task(task_id)
            self.freedom_panels.pop(task_id, None)
            self.freedom_logs.pop(task_id, None)
            self.freedom_notebook.forget(index)

            # 删除磁盘上的任务目录，防止下次启动重新加载
            try:
                task_dir = ROUND_BASE_DIR / task_name
                if task_dir.exists():
                    shutil.rmtree(task_dir)
            except Exception as e:
                self.log_global(f"[编排] 删除任务目录失败: {e}", sensitive=True)

            self._loaded_freedom_task_names.discard(task_name)

            self.log_global(f"[编排] 已删除任务：{task_name}")
            self._refresh_all_phase3_panels()

    def _add_phase3_task(self, task_name=None):
        if not task_name:
            task_name = simpledialog.askstring("新增计划任务", "请输入任务名称：", parent=self.root)
            if not task_name: return
        task_name = task_name.strip()

        task_dir = PHASE3_BASE_DIR / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        config_file = task_dir / "config.json"
        config = {
            "task_name": task_name,
            "created_at": datetime.now().isoformat(),
            "skill_name": "",
            "linked_freedom_task": "",
            "current_round": 1
        }
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        from ui.phase3_task_panel import Phase3TaskPanel
        tab_frame = Frame(self.phase3_notebook)
        panel = Phase3TaskPanel(tab_frame, app=self, task_name=task_name,
                                get_hosts_fn=self.list_subpages)
        panel.pack(fill="both", expand=True)
        task_id = f"phase3_{task_name}_{int(time.time())}"
        panel.task_id = task_id
        self.phase3_panels[task_id] = panel
        self.phase3_notebook.add(tab_frame, text=task_name)
        tab_frame.panel = panel
        self.log_global(f"[Phase3] 任务已创建：{task_dir}")

    def get_freedom_tasks_info(self):
        """返回编排台所有任务的信息列表 [{name, host, round}]"""
        tasks = []
        for task_id, panel in self.freedom_panels.items():
            try:
                host = panel.selected_host_var.get().strip()
            except Exception:
                host = ""
            tasks.append({
                "name": panel.task_name,
                "host": host,
                "round": panel.current_round
            })
        return tasks

    def _load_phase3_tasks(self):
        """从硬盘恢复决策台任务"""
        if not PHASE3_BASE_DIR.exists():
            return
        for task_folder in PHASE3_BASE_DIR.iterdir():
            if not task_folder.is_dir():
                continue
            task_name = task_folder.name
            config_file = task_folder / "config.json"
            if not config_file.exists():
                continue
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                from ui.phase3_task_panel import Phase3TaskPanel
                tab_frame = Frame(self.phase3_notebook)
                panel = Phase3TaskPanel(tab_frame, app=self, task_name=task_name,
                                        get_hosts_fn=self.list_subpages)
                panel.pack(fill="both", expand=True)
                task_id = f"phase3_{task_name}_{int(time.time())}"
                panel.task_id = task_id

                # 延迟恢复配置：等任务列表加载完成后再设置
                saved_skill = config.get("skill_name", "")
                saved_task = config.get("linked_freedom_task", "")
                def restore_config(p=panel, sk=saved_skill, tk=saved_task):
                    if sk and hasattr(p, 'skill_combo') and sk in p.skill_combo['values']:
                        p.skill_var.set(sk)
                    if tk and hasattr(p, 'task_combo') and tk in p.task_combo['values']:
                        p.task_var.set(tk)
                        p._on_task_selected()
                # 延迟500ms执行恢复，确保下拉框数据已加载
                panel.after(500, restore_config)

                self.phase3_panels[task_id] = panel
                self.phase3_notebook.add(tab_frame, text=task_name)
                tab_frame.panel = panel
                self.log_global(f"[Phase3] 已恢复任务：{task_name}")
            except Exception as e:
                self.log_global(f"[Phase3] 恢复任务失败 {task_name}：{e}")

    def save_phase3_config(self, task_name, skill_name=None, linked_task=None, current_round=None):
        """保存决策台任务的当前配置到硬盘"""
        task_dir = PHASE3_BASE_DIR / task_name
        if not task_dir.exists():
            return
        config_file = task_dir / "config.json"
        config = {}
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        if skill_name is not None:
            config["skill_name"] = skill_name
        if linked_task is not None:
            config["linked_freedom_task"] = linked_task
        if current_round is not None:
            config["current_round"] = current_round
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    def _remove_phase3_task(self, index):
        tabs = self.phase3_notebook.tabs()
        if 0 <= index < len(tabs):
            tab_frame = tabs[index]
            panel = getattr(tab_frame, 'panel', None)
            if panel and hasattr(panel, 'task_id'):
                task_name = panel.task_name
                self.phase3_panels.pop(panel.task_id, None)
                task_dir = PHASE3_BASE_DIR / task_name
                if task_dir.exists():
                    shutil.rmtree(task_dir)
            self.phase3_notebook.forget(index)

    def _refresh_all_phase3_panels(self):
        """通知所有决策台面板立即刷新关联任务数据"""
        for panel in self.phase3_panels.values():
            try:
                panel._refresh_freedom_tasks()
            except Exception:
                pass

    def _write_log(self, line: str):
        if not self.project_dir:
            return
        self._log_writer.write(line)

    def _format_log(self, msg: str, host: str = None) -> str:
        ts = timestamp()
        if host: return f"[{ts}] [{host}] {msg}\n"
        return f"[{ts}] {msg}\n"

    def log_global(self, msg, host=None, sensitive=False):
        line = self._format_log(msg)
        self.root.after(0, lambda: self.global_log.insert(END, line))
        self.root.after(0, lambda: self.global_log.see(END))
        if not sensitive:
            with self._log_lock:
                self.recent_log.append(line.rstrip('\n'))
                if len(self.recent_log) > 20: self.recent_log.pop(0)
        self._write_log(line)
        if host: self._log_to_target_gui(host, msg)

    def log_target(self, host, msg):
        line = self._format_log(msg)
        self._log_to_target_gui(host, line)
        self._write_log(self._format_log(msg, host=host))

    def log_target_program(self, host, msg):
        line = self._format_log(msg)
        self._log_to_program_gui(host, line)
        self._write_log(self._format_log(msg, host=host))

    def _log_to_target_gui(self, host, line):
        with self.targets_lock:
            w = self.target_logs.get(host)
        if w:
            self.root.after(0, lambda: w.insert(END, line))
            self.root.after(0, lambda: w.see(END))

    def _log_to_program_gui(self, host, line):
        with self.targets_lock:
            w = self.target_program_logs.get(host)
        if w:
            self.root.after(0, lambda: w.insert(END, line))
            self.root.after(0, lambda: w.see(END))

    def send_chat(self, event=None):
        msg = self.chat_input.get("1.0", END).strip()
        if not msg: return
        self.chat_input.delete("1.0", END)
        ctx = f"项目：{self.project_name or '无'}\n最近日志：\n{self.get_recent_context()}"
        self.display_chat("你", msg)
        def run_autonomous():
            self.orchestrator.run_autonomous(msg, ctx)
        threading.Thread(target=run_autonomous, daemon=True).start()

    def display_chat(self, sender, text):
        self.chat_log.config(state=NORMAL)
        self.chat_log.insert(END, f"{sender}: {text}\n")
        self.chat_log.see(END)
        self.chat_log.config(state=DISABLED)

    def get_recent_context(self):
        return "\n".join(self.recent_log[-10:]) if self.recent_log else "暂无日志"

    def save_api_key(self):
        key = self.api_entry.get().strip()
        if not key or all(c == '*' for c in key):
            return
        if not self.key_mgr.cipher:
            messagebox.showwarning("提示", "cryptography 未安装，无法加密保存 API Key")
            return
        self.key_mgr.save(key)
        self.stored_key = key
        self.api_entry.delete(0, END)
        self.api_entry.insert(0, self.key_mgr.mask_key(key))
        self._rebuild_ai_client()
        self.log_global("API Key 已加密保存")

    def _rebuild_ai_client(self):
        """按当前 API Key 与模型重建客户端与编排器（两者必须同时重建）。"""
        self.chat_client = DeepSeekClient(self.stored_key, model=self.current_model)
        self.orchestrator = AgentOrchestrator(
            self.chat_client,
            on_message=lambda sender, text: self.root.after(0, lambda: self.display_chat(sender, text)),
            app=self,
        )
        self._register_orchestrator_actions()

    def _load_model_config(self):
        config_file = PROJECTS_DIR.parent / "model_config.json"
        self.current_model = DEFAULT_MODEL
        if not config_file.exists():
            return
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                saved = json.load(f).get("model")
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if saved in MODEL_IDS:
            self.current_model = saved

    def _save_model_config(self):
        config_file = PROJECTS_DIR.parent / "model_config.json"
        try:
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump({"model": self.current_model}, f)
        except OSError:
            pass

    def _on_model_change(self):
        self.current_model = self.current_model_var.get()
        self._save_model_config()
        self._apply_model_change()

    def _apply_model_change(self):
        if self.current_model not in MODEL_IDS:
            self.current_model = DEFAULT_MODEL
            self.current_model_var.set(self.current_model)
        self.chat_title.config(text=f"{model_label(self.current_model)} 助手")
        if not self.stored_key:
            messagebox.showwarning("提示", "请先保存 API Key")
            return
        if any(t.is_alive() for t in self.target_threads.values()):
            if not messagebox.askyesno("警告", "当前有运行中的任务，切换模型会中断所有任务。是否继续？"):
                self._load_model_config()
                self.current_model_var.set(self.current_model)
                return
            self.stop_all_scans()
        self._rebuild_ai_client()
        self.log_global(f"模型已切换: {model_label(self.current_model)}")

    def store_tool_output(self, host, tool_name, lines):
        with self.targets_lock:
            if host not in self.collected_data:
                self.collected_data[host] = {}
            self.collected_data[host][tool_name] = lines

    def run_global_analysis(self):
        if not self.collected_data:
            messagebox.showinfo("提示", "还没有任何信息收集数据")
            return
        with self.targets_lock:
            raw_parts = []
            for host, tools in self.collected_data.items():
                raw_parts.append(f"\n--- 目标：{host} ---")
                for tname, lines in tools.items():
                    raw = "\n".join(lines)
                    clean = clean_ansi(raw)
                    snippet = clean[-1500:] if len(clean) > 1500 else clean
                    raw_parts.append(f"【{tname} 输出】\n{snippet}")
            combined_raw = "\n".join(raw_parts)
        existing = self.summary_log.get("1.0", "end")
        pre_analysis = "\n".join(existing.splitlines()[-30:])
        context = f"""项目：{self.project_name or '无'}
=== 主界面已有分析结论 ===
{pre_analysis}
=== 工具原始输出 ===
{combined_raw}
"""
        self.summary_log.insert("end", "\n" + "=" * 60 + "\n🔍 正在进行汇总分析...\n")
        self.summary_log.see("end")
        def analyze():
            prompt = ("请基于以上所有信息，产出一份汇总报告。要求："
                      "1) 概述整体情况；2) 指出值得关注的异常或风险；"
                      "3) 给出按优先级排序的下一步建议。")
            result = self.chat_client.send(prompt, context)
            self.root.after(0, lambda: self.summary_log.insert("end", result + "\n"))
            self.root.after(0, lambda: self.summary_log.see("end"))
        threading.Thread(target=analyze, daemon=True).start()
