"""main.py - 程序入口。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tkinter as tk

from core.api_key_manager import APIKeyManager
from app import SOSWorkbenchApp


def main():
    root = tk.Tk()
    key_mgr = APIKeyManager()
    app = SOSWorkbenchApp(root, key_mgr)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_closing()


if __name__ == "__main__":
    main()
