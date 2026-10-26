"""ui/main_panel.py - 主界面面板：全局日志 + 结果汇总。"""

from tkinter import Frame, Label, Button, scrolledtext, PanedWindow

class MainPanel(Frame):
    """主界面标签页的内容面板（是一个 Frame）"""

    def __init__(self, parent, analyze_callback=None):
        # 关键：正确调用父类 Frame 的构造函数，让 MainPanel 成为一个真正的 Frame 控件
        super().__init__(parent)

        # 创建垂直分割面板（作为 self 的子控件）
        main_pane = PanedWindow(self, orient="vertical", sashrelief="raised", sashwidth=6)
        main_pane.pack(fill="both", expand=True)

        # 上半部分：全局运行日志
        log_frame = Frame(main_pane)
        # 只改这一行！加 height=420，全局日志直接变大一点点
        main_pane.add(log_frame, stretch="always", height=500)
        Label(log_frame, text="全局运行日志", font=("微软雅黑", 9, "bold")).pack(anchor="w")
        self.global_log = scrolledtext.ScrolledText(log_frame, wrap="word")
        self.global_log.pack(fill="both", expand=True)

        # 下半部分：信息汇总与全局研判
        summary_frame = Frame(main_pane)
        main_pane.add(summary_frame, stretch="always")
        header_frame = Frame(summary_frame)
        header_frame.pack(fill="x")
        Label(header_frame, text="结果汇总与分析", font=("微软雅黑", 9, "bold")).pack(side="left")
        self.analyze_btn = Button(header_frame, text="🔍 汇总分析",
                                  command=analyze_callback if analyze_callback else lambda: None,
                                  state="disabled")
        self.analyze_btn.pack(side="right", padx=5)
        self.summary_log = scrolledtext.ScrolledText(summary_frame, wrap="word")
        self.summary_log.pack(fill="both", expand=True)
        self.summary_log.insert("end", "各步骤的结果汇总与分析结论将在此显示...\n")