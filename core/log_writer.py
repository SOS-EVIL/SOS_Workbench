"""
core/log_writer.py - 全局文本日志（session.log）的异步写入器

为什么单独抽出来：
    旧 app.py 的 _ensure_log_writer() 用一个在 __init__ 里已经赋值过的条件做判断
    （if not hasattr(self,'_log_buffer')），导致写入线程**从未启动**：
      · session.log 从来不会生成
      · _log_buffer 无界增长（长时间运行内存持续上涨）
    这里改成独立、有界、可单测的组件，并且不依赖 Tk。

行为：
    write() 只入队；后台线程每 flush_interval 秒批量落盘；close() 保证收尾。
"""

import collections
import threading
from pathlib import Path

class LogWriter:
    def __init__(self, project_dir_getter, flush_interval=2.0, maxlen=20000):
        """
        Args:
            project_dir_getter: 无参可调用，返回当前项目目录（可能为 None）
            flush_interval:     批量落盘间隔（秒）
            maxlen:             缓冲区上限，超出丢弃最旧的行（防内存泄漏）
        """
        self._get_dir = project_dir_getter
        self._interval = flush_interval
        self._buf = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._start_lock = threading.Lock()
        self.dropped = 0

    def _ensure_started(self):
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="LogWriter", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.wait(self._interval):
            self.flush()

    def write(self, line: str):
        self._ensure_started()
        with self._lock:
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1
            self._buf.append(line)

    def flush(self) -> int:
        """把缓冲写入 {project_dir}/session.log，返回写入行数。"""
        directory = self._get_dir()
        if not directory:
            return 0
        with self._lock:
            if not self._buf:
                return 0
            lines = list(self._buf)
            self._buf.clear()
        try:
            path = Path(directory) / "session.log"
            with open(path, "a", encoding="utf-8", buffering=8192) as f:
                f.writelines(lines)
        except OSError:
            return 0
        return len(lines)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.flush()

    def pending(self) -> int:
        with self._lock:
            return len(self._buf)
