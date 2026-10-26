"""ui/custom_tab.py - 可横向滚动的标签栏，通过回调与外部交互。"""

from tkinter import Frame, Canvas, Scrollbar, Button

class CustomTabNotebook(Frame):
    """可横向滚动的自定义标签栏，支持鼠标滚轮和右侧 +/- 按钮回调。"""

    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent)
        self._tabs = []
        self.current_tab = None

        self.top_container = Frame(self, height=40, bg="#f0f0f0")
        self.top_container.pack(side="top", fill="x")
        self.top_container.pack_propagate(False)

        self.canvas_frame = Frame(self.top_container, height=28, bg="#f0f0f0")
        self.canvas_frame.pack(side="top", fill="x")
        self.canvas_frame.pack_propagate(False)

        self.canvas = Canvas(self.canvas_frame, highlightthickness=0, bg="#f0f0f0")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.scrollbar = Scrollbar(self.top_container, orient="horizontal", width=10)
        self.scrollbar.pack(side="bottom", fill="x", pady=(0, 0))
        self.canvas.configure(xscrollcommand=self.scrollbar.set)
        self.scrollbar.configure(command=self.canvas.xview)

        self.tabs_inner = Frame(self.canvas, bg="#f0f0f0")
        self.canvas.create_window((0, 0), window=self.tabs_inner, anchor="nw", tags="tabs")

        self.remove_btn = Button(self.canvas_frame, text="－", font=("微软雅黑", 12, "bold"),
                                 command=self._on_remove_click,
                                 width=2, height=1, relief="raised", bg="#e0e0e0")
        self.remove_btn.pack(side="right", padx=(2, 0))

        self.add_btn = Button(self.canvas_frame, text="＋", font=("微软雅黑", 12, "bold"),
                              command=self._on_add_click,
                              width=2, height=1, relief="raised", bg="#e0e0e0")
        self.add_btn.pack(side="right", padx=(2, 0))

        self.content_area = Frame(self)
        self.content_area.pack(side="top", fill="both", expand=True)

        self.tabs_inner.bind("<Configure>", self._update_scroll)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self.add_callback = None
        self.remove_callback = None

    def set_add_callback(self, callback):
        self.add_callback = callback

    def set_remove_callback(self, callback):
        self.remove_callback = callback

    def _on_add_click(self):
        if self.add_callback:
            self.add_callback()

    def _on_remove_click(self):
        if self.remove_callback and self.current_tab is not None:
            self.remove_callback(self.current_tab)

    def _select_by_button(self, btn):
        for idx, tab in enumerate(self._tabs):
            if tab["btn"] == btn:
                self._select(idx)
                return

    def add(self, frame, text=None):
        btn = Button(self.tabs_inner, text=text or "", font=("微软雅黑", 9),
                     relief="flat", bg="#f0f0f0", padx=12, pady=4, height=1)
        btn.pack(side="left", fill="y", padx=1)
        btn.config(command=lambda b=btn: self._select_by_button(b))
        self._tabs.append({"text": text, "frame": frame, "btn": btn})
        if len(self._tabs) == 1:
            self._select(0)
        self._update_scroll()

    def forget(self, index):
        if 0 <= index < len(self._tabs):
            self._tabs[index]["btn"].destroy()
            self._tabs[index]["frame"].destroy()
            del self._tabs[index]
            if self.current_tab == index or self.current_tab >= len(self._tabs):
                if self._tabs:
                    self._select(0)
                else:
                    self.current_tab = None
            self._update_scroll()

    def _select(self, index):
        if 0 <= index < len(self._tabs):
            if self.current_tab is not None and 0 <= self.current_tab < len(self._tabs):
                self._tabs[self.current_tab]["btn"].config(bg="#f0f0f0", relief="flat")
                self._tabs[self.current_tab]["frame"].pack_forget()
        self._tabs[index]["btn"].config(bg="#d0d0d0", relief="sunken")
        self._tabs[index]["frame"].pack(fill="both", expand=True)
        self.current_tab = index

        # 从当前标签页一路往上找，强制整个容器链完成几何计算
        frame = self._tabs[index]["frame"]
        w = frame
        while w is not None:
            try:
                w.update_idletasks()
            except Exception:
                pass
            try:
                w = w.master
            except Exception:
                w = None

    def _update_scroll(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_resize(self, event):
        pass

    def _on_mousewheel(self, event):
        self.canvas.xview_scroll(int(-1 * (event.delta / 60)), "units")

    def tabs(self):
        return [tab["frame"] for tab in self._tabs]

    def select(self, index):
        self._select(index)