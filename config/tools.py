"""config/tools.py - 全局配置常量。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 运行数据根目录。默认 storage/；CLI/测试可用 SOS_DATA_DIR 隔离。
DATA_DIR = Path(os.environ.get("SOS_DATA_DIR") or (BASE_DIR / "storage")).expanduser().resolve()

# 工具目录。平台只要求"能被命令行调起"，不限定语言。
# DATA_DIR/tools 是 CLI 审核通过的托管工具目录。
MANAGED_TOOLS_DIR = DATA_DIR / "tools"
MANAGED_SKILLS_DIR = DATA_DIR / "skills"
STAGING_DIR = DATA_DIR / "staging"
TOOL_DIRS = [BASE_DIR / "tools", BASE_DIR / "external_tools", MANAGED_TOOLS_DIR]
SKILLS_DIRS = [BASE_DIR / "skills", MANAGED_SKILLS_DIR]

# 可执行工具后缀。.py/.pyz 用当前解释器启动，其余按原生方式启动。
TOOL_EXTENSIONS = (".py", ".pyz", ".exe", ".bat", ".cmd", ".ps1", ".sh", ".js")

DEFAULT_CONCURRENCY = 3

# 单步默认超时（秒），可在步骤参数里用 "timeout" 覆盖。
DEFAULT_STEP_TIMEOUT = 900

# 是否把 shell_exec 注册为模型可调用的动作。
# 默认关闭：把任意命令执行交给模型等于通用 RCE 分发器。
ENABLE_SHELL_ACTION = False

# 是否允许模型在对话中直接触发工具动作（run_tool / start_target 等）。
# 默认关闭：SOS 是人工触发的执行台，模型只应给出建议，不应直接操作。
ENABLE_MODEL_ACTIONS = False

# 是否显示「鲸吞/栖鲸」自动模式。
# 默认关闭：SOS 的定位是人工触发的下游执行台，不是自主循环。
# 打开后仍是实验性功能，不会改变本文件之外的安全默认值。
ENABLE_AUTO_MODE = False


def get_tools():
    tools = {}
    for directory in TOOL_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith("_"):
                continue
            if path.suffix.lower() in TOOL_EXTENSIONS:
                tools[path.stem] = str(path)
    return tools

TOOLS = get_tools()

PROJECTS_DIR = DATA_DIR / "projects"
ROUND_BASE_DIR = DATA_DIR / "round"
PHASE3_BASE_DIR = DATA_DIR / "phase3"
RUNS_DIR = DATA_DIR / "runs"


def ensure_data_dirs():
    """在应用启动时创建运行数据目录。

    不在 import 时创建，避免仅导入测试/工具时就在仓库里留下 storage/。
    """
    for directory in (
        PROJECTS_DIR, ROUND_BASE_DIR, PHASE3_BASE_DIR, RUNS_DIR,
        MANAGED_TOOLS_DIR, MANAGED_SKILLS_DIR, STAGING_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
