"""
ui/target_panel.py - 单目标面板（控制栏 + 双日志框）
纯 UI 组件，通过回调与外部交互。不包含任何业务控制逻辑。
"""

from tkinter import Frame, Label, Entry, Button, StringVar, scrolledtext, PanedWindow

class TargetPanel:
    """单个目标的操作面板，包含控制栏、目标日志和程序运行情况。"""

    def __init__(self, parent, host, on_toggle_scan, on_stop_scan, on_kill_scan):
        """
        Args:
            parent: 父级 tkinter 容器
            host: 目标主机名（URL 只读显示）
            on_toggle_scan: 点击「开始/暂停」按钮的回调 (host, state_var) -> None
            on_stop_scan: 点击「停止」按钮的回调 (host) -> None
            on_kill_scan: 点击「杀死」按钮的回调 (host) -> None
        """
        self.host = host
        self._toggle_callback = on_toggle_scan
        self._stop_callback = on_stop_scan
        self._kill_callback = on_kill_scan

        # 控制栏（整体结构不变）
        control_bar = Frame(parent)
        control_bar.pack(side="top", fill="x", padx=5, pady=2)

        # 任务控制按钮组（完全不变）
        self.state_var = StringVar(value="▶ 开始")
        btn_start = Button(control_bar, textvariable=self.state_var, width=8,
                           command=lambda: self._toggle_callback(host, self.state_var))
        btn_start.pack(side="left", padx=2)

        btn_stop = Button(control_bar, text="■ 停止", width=6,
                          command=lambda: self._stop_callback(host))
        btn_stop.pack(side="left", padx=2)

        btn_kill = Button(control_bar, text="★ 杀死", width=6,
                          command=lambda: self._kill_callback(host))
        btn_kill.pack(side="left", padx=2)

        # 右侧：URL 只读展示区（自动占满剩余空间）
        right_container = Frame(control_bar)
        right_container.pack(side="left", fill="both", expand=True, padx=(5, 0))

        # URL 只读行（功能完全不变，现在自动占满整个右侧宽度）
        url_var = StringVar(value=host)
        url_entry = Entry(right_container, textvariable=url_var, state="readonly",
                          readonlybackground="white", relief="sunken")
        url_entry.pack(fill="both", expand=True)

        # 垂直分隔日志区域（完全不变）
        pane = PanedWindow(parent, orient="vertical", sashrelief="raised", sashwidth=6, bg="#cccccc")
        pane.pack(fill="both", expand=True, padx=5, pady=(2, 0))

        # 上方：目标日志
        log_frame = Frame(pane)
        pane.add(log_frame, minsize=300,height=400, stretch="always")
        Label(log_frame, text="目标日志", font=("微软雅黑", 9, "bold")).pack(anchor="w")
        self.log_widget = scrolledtext.ScrolledText(log_frame, wrap="word")
        self.log_widget.pack(fill="both", expand=True)

        # 下方：程序运行情况
        prog_frame = Frame(pane)
        pane.add(prog_frame, minsize=300, stretch="always")
        Label(prog_frame, text="程序运行情况", font=("微软雅黑", 9, "bold")).pack(anchor="w")
        self.prog_widget = scrolledtext.ScrolledText(prog_frame, wrap="word")
        self.prog_widget.pack(fill="both", expand=True)