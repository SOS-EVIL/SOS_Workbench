"""core/tool_registry.py - 工具发现与参数能力探测。

工具可以是任意语言：.py/.pyz 会被解析源码探测参数，其余后缀按平台契约处理。
"""

import json
import re
from pathlib import Path

from config.tools import TOOL_DIRS, TOOL_EXTENSIONS

_ARG_RE = re.compile(r"""add_argument\(\s*['"](--[A-Za-z0-9_-]+)['"]""")

# 只声明旧契约的工具（探测失败时的兜底）
LEGACY_ARGS = frozenset({"--project-dir", "--host"})

# 完整平台契约（非 Python 工具无法探测源码，按此下传）
CONTRACT_ARGS = frozenset({"--project-dir", "--target", "--config"})

class ToolRegistry:
    def __init__(self, tool_dirs=None):
        if tool_dirs is None:
            self.tool_dirs = list(TOOL_DIRS)
        else:
            self.tool_dirs = [Path(d) for d in tool_dirs]
        self._tools = {}
        self._info_cache = {}
        self._arg_cache = {}
        self.refresh()

    def refresh(self):
        self._tools.clear()
        self._arg_cache.clear()
        for directory in self.tool_dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.name.startswith("_"):
                    continue
                if path.suffix.lower() in TOOL_EXTENSIONS:
                    self._tools[path.stem] = str(path)

    def get_all_tools(self):
        return dict(self._tools)

    def args_of(self, tool_name):
        """返回工具接受的长参数集合，决定命令行的下传方式。"""
        path = self._tools.get(tool_name)
        if not path:
            return frozenset()

        manifest_args = self._manifest_args(path)
        if manifest_args:
            return manifest_args

        if Path(path).suffix.lower() not in (".py", ".pyz"):
            return CONTRACT_ARGS

        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            return frozenset()

        cached = self._arg_cache.get(tool_name)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                args = frozenset(_ARG_RE.findall(f.read()))
        except OSError:
            args = frozenset()

        self._arg_cache[tool_name] = (mtime, args)
        return args

    @staticmethod
    def _manifest_args(path):
        """同名 .json 里的 {"args": ["--x", ...]} 可覆盖探测结果。"""
        manifest = Path(path).with_suffix(".json")
        if not manifest.exists():
            return frozenset()
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return frozenset()
        args = meta.get("args") if isinstance(meta, dict) else None
        if isinstance(args, list) and args:
            return frozenset(str(a) for a in args)
        return frozenset()

    def accepts_config(self, tool_name):
        return "--config" in self.args_of(tool_name)

    def accepts_target(self, tool_name):
        return "--target" in self.args_of(tool_name)

    def get_tool_info(self, tool_name):
        if tool_name in self._info_cache:
            return self._info_cache[tool_name]
        path = self._tools.get(tool_name)
        if not path:
            return None
        info = {
            "name": tool_name,
            "path": path,
            "description": "",
            "parameters": [],
            "args": sorted(self.args_of(tool_name)),
        }
        manifest = Path(path).with_suffix(".json")
        if manifest.exists():
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8-sig"))
                if isinstance(meta, dict):
                    info.update(meta)
            except (OSError, json.JSONDecodeError):
                pass
        self._info_cache[tool_name] = info
        return info

    def list_tools_for_ai(self):
        out = []
        for name in self._tools:
            info = self.get_tool_info(name) or {}
            out.append({
                "name": name,
                "description": info.get("description", ""),
                "parameters": info.get("parameters", []),
            })
        return out
