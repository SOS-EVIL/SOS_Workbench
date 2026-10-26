"""core/shell_executor.py - 轻量命令执行器：带超时，返回合并输出。"""

import subprocess
import os

class ShellExecutor:
    """执行临时 shell 命令，并返回结果"""

    @staticmethod
    def run(command: str, timeout: int = 15) -> str:
        """
        执行一条 shell 命令，返回合并后的输出。
        超时或异常时返回错误描述。
        """
        try:
            # 使用 shell=True 以支持管道等复杂命令
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=dict(os.environ, PYTHONUNBUFFERED="1")
            )
            output = proc.stdout.strip()
            if proc.stderr.strip():
                output += "\n[stderr]\n" + proc.stderr.strip()
            if not output:
                output = f"(命令执行完毕，无输出，返回码 {proc.returncode})"
            return output
        except subprocess.TimeoutExpired:
            return f"命令执行超时（{timeout}秒）"
        except FileNotFoundError:
            return "错误：未找到指定的命令或程序"
        except Exception as e:
            return f"命令执行异常：{str(e)}"