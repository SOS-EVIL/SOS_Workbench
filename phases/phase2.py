"""phases/phase2.py - 二级编排：步骤链、参数继承、轮次汇总。"""

import threading, json, time, subprocess, sys
from core import targets as target_api
from core.tool_runner import child_env
from utils.log_utils import clean_ansi
from datetime import datetime
from pathlib import Path
from config.tools import ROUND_BASE_DIR, DEFAULT_STEP_TIMEOUT

# 平台注入的保留参数键：不是"工具不认识"，告警时应排除
PLATFORM_PARAM_KEYS = {"inherit_data", "timeout", "round", "task_name", "category"}


def _extract_json_result(lines):
    """从工具输出行中取出 stdout 的 JSON 结果块；失败返回 None。"""
    try:
        start = lines.index("__JSON_RESULT_START__")
        end = lines.index("__JSON_RESULT_END__")
        return json.loads("\n".join(lines[start + 1:end]))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

class _ToolCallbacks:
    def __init__(self, phase, host, panel, tool_name, round_num=None, task_name=None, exec_index=1):
        self.phase = phase
        self.host = host
        self.panel = panel
        self.tool_name = tool_name
        self.round_num = round_num or 1
        self.task_name = task_name or "default"
        self.exec_index = exec_index
        self.start_time = time.time()
        self._output_lines = []
        self._completed = threading.Event()
        self._result = ""
        self.app = phase.app

    def _get_round_latest_dir(self):
        project_dir = getattr(self.app, 'project_dir', None)
        if not project_dir:
            return None
        host_safe = target_api.slug(self.host)
        round_dir = Path(project_dir) / "latest" / host_safe / f"round_{self.round_num}"
        round_dir.mkdir(parents=True, exist_ok=True)
        return round_dir

    def on_log_line(self, host, line):
        clean_line = clean_ansi(line)
        self.panel.append_log(f"[{self.tool_name}] {clean_line}")
        self.app.log_target_program(self.host, f"[{self.tool_name}] {clean_line}")
        if hasattr(self.app, 'structured_logger'):
            self.app.structured_logger.log_tool_output(
                host=self.host, tool=self.tool_name, line=clean_line
            )
        self._output_lines.append(clean_line)

    def on_tool_complete(self, host, tool_name, collected_lines):
        duration = time.time() - self.start_time
        status = "success"
        for line in collected_lines:
            if line.startswith("__ERROR__"):
                status = "failed"
                break

        if hasattr(self.app, 'structured_logger'):
            self.app.structured_logger.log_tool_end(
                host=host, tool=tool_name, status=status, duration=duration,
                result_summary=f"{len(collected_lines)} lines"
            )

        self.app.store_tool_output(host, tool_name, collected_lines)
        self.app.log_target(host, f"✅ {tool_name} 执行完成")
        self._save_tool_latest(host, tool_name, status, duration)

        self._result = "\n".join(self._output_lines)
        self._completed.set()

    def _save_tool_latest(self, host, tool_name, status, duration):
        round_dir = self._get_round_latest_dir()
        if not round_dir:
            return
        filename = f"{tool_name}_{self.exec_index}.json"
        latest_file = round_dir / filename
        data = {
            "tool": tool_name,
            "host": host,
            "status": status,
            "round": self.round_num,
            "task_name": self.task_name,
            "exec_index": self.exec_index,
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(timespec='seconds'),
            "end_time": datetime.now().isoformat(timespec='seconds'),
            "duration": round(duration, 2),
            "output_lines": self._output_lines.copy()
        }
        try:
            with open(latest_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def wait_for_completion(self, timeout=None):
        self._completed.wait(timeout=timeout)
        # 返回字典，包含状态和输出
        return {
            "success": not any(line.startswith("__ERROR__") for line in self._output_lines),
            "output": self._result,
            "output_lines": self._output_lines,
            "duration": round(time.time() - self.start_time, 2)
        }

class Phase2Freedom:
    def __init__(self, app):
        self.app = app
        self.task_executor = app.executor
        self.tool_registry = app.tool_registry
        self.running_tasks = {}
        self.task_data = {}

    def run_task(self, task_id, panel, target_host):
        if not target_host:
            panel.append_log("[ERROR] 请选择目标目标！")
            return
        if task_id in self.running_tasks:
            task_info = self.running_tasks[task_id]
            if task_info["is_running"]:
                pe = task_info.get("pause_event")
                if pe and pe.is_set():
                    pe.clear()
                    panel.append_log("[INFO] 任务继续执行...")
                    return
                else:
                    panel.append_log("[INFO] 任务正在运行中...")
                    return

        plan_data = panel.get_tool_boxes_data()
        tool_chain = plan_data.get("tools", [])
        if not tool_chain:
            panel.append_log("[ERROR] 未添加任何工具/技能！")
            return

        pause_event = threading.Event()
        self.app.register_freedom_thread(target_host, pause_event)

        current_round = panel.current_round
        task_name = panel.task_name

        self.running_tasks[task_id] = {
            "is_running": True,
            "thread": None,
            "panel": panel,
            "host": target_host,
            "pause_event": pause_event,
            "current_round": current_round,
            "task_name": task_name
        }
        self.task_data[task_id] = {}

        task_thread = threading.Thread(
            target=self._execute_tool_chain,
            args=(task_id, panel, target_host, tool_chain, pause_event, current_round, task_name),
            daemon=True
        )
        self.running_tasks[task_id]["thread"] = task_thread
        task_thread.start()
        panel.append_log(f"[SUCCESS] 任务启动成功 | 目标：{target_host} | 第{current_round}轮")

    def stop_task(self, task_id):
        if task_id in self.running_tasks:
            pe = self.running_tasks[task_id].get("pause_event")
            if pe: pe.set()
            panel = self.running_tasks[task_id]["panel"]
            panel.append_log("[STOP] 任务已暂停")

    def kill_task(self, task_id):
        if task_id in self.running_tasks:
            task_info = self.running_tasks[task_id]
            self.running_tasks[task_id]["is_running"] = False
            host = task_info["host"]
            pe = task_info.get("pause_event")
            if pe: pe.set()
            self._save_round_summary(task_info.get("task_name", "default"),
                                     task_info.get("current_round", 1),
                                     self.task_data.get(task_id, {}))
            self.app.unregister_freedom_thread(host)
            self.app.phase1.kill_execution(host)
            panel = task_info["panel"]
            panel.append_log("[KILL] 任务已强制终止！所有进程已清理")

    def _execute_tool_chain(self, task_id, panel, host, tool_chain, pause_event, round_num, task_name):
        # round_results: { tool_name: [dict1, dict2, ...] }
        round_results = {}
        try:
            for index, tool in enumerate(tool_chain):
                if not self.running_tasks[task_id]["is_running"]: break
                while pause_event.is_set() and self.running_tasks[task_id]["is_running"]:
                    time.sleep(0.2)
                if not self.running_tasks[task_id]["is_running"]: break

                tool_name = tool["name"]
                tool_type = tool.get("type", "tool")
                raw_params = tool["params"]
                inherit_from = tool["inherit_from"]
                inherit_file = tool.get("inherit_file")

                panel.append_log(f"\n====== 执行第 {index+1} 步：{tool_name} ({tool_type}) ======")

                exec_index = len(round_results.get(tool_name, [])) + 1

                # 1) 解析参数 → 2) 先脱敏留一份给日志 → 3) 再解析凭证给执行 → 4) 计算超时
                raw_params_obj = self._parse_parameters(task_id, raw_params, inherit_from, inherit_file)
                display_params = target_api.redact(raw_params_obj)
                try:
                    params = target_api.resolve_secrets(raw_params_obj)
                except Exception as e:
                    panel.append_log(f"[ERROR] 凭据解析失败：{e}")
                    round_results.setdefault(tool_name, []).append(
                        {"success": False, "error": str(e), "exec_index": exec_index}
                    )
                    continue
                timeout = self._step_timeout(params)

                if tool_type == "skill":
                    result = self._run_skill(
                        tool_name, host, params, panel, pause_event, round_num, task_name, exec_index,
                    )
                elif "freedom" in tool_name.lower():
                    result = self._run_freedom_atomic(
                        tool_name, host, params, panel, pause_event, round_num,
                        task_name, exec_index, timeout=timeout, display_params=display_params,
                    )
                else:
                    result = self._run_single_tool(tool_name, host, panel, pause_event,
                                                   params=params, display_params=display_params,
                                                   round_num=round_num,
                                                   task_name=task_name, exec_index=exec_index,
                                                   timeout=timeout)

                # 结果统一为字典，追加到列表，并写回 task_data 供后续步骤 inherit_from
                round_results.setdefault(tool_name, []).append(result)
                self.task_data.setdefault(task_id, {})[tool_name] = result
                panel.append_log(f"[SUCCESS] {tool_name} 执行完成")

            def finish_round():
                panel.append_log("\n====== 任务全部执行完成 ======")
                self._save_round_summary(task_name, round_num, round_results)
                panel.advance_round()
                panel.append_log(f"[INFO] 已进入第 {panel.current_round} 轮")

            self.app.root.after(0, finish_round)

        except Exception as e:
            panel.append_log(f"[ERROR] 任务执行失败：{str(e)}")
            self._save_round_summary(task_name, round_num, round_results)
        finally:
            if task_id in self.running_tasks:
                self.running_tasks[task_id]["is_running"] = False
                self.app.unregister_freedom_thread(host)

    def _save_round_summary(self, task_name, round_num, round_results):
        if not ROUND_BASE_DIR or not task_name: return
        task_dir = ROUND_BASE_DIR / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        summary_file = task_dir / f"{task_name}_round_{round_num}.json"

        summary = {
            "task_name": task_name,
            "round": round_num,
            "timestamp": datetime.now().isoformat(timespec='seconds'),
            "tools": round_results
        }
        try:
            with open(summary_file, 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if hasattr(self.app, 'log_global'):
                self.app.log_global(f"[编排] 轮次汇总保存失败: {e}", sensitive=True)

    # 工具执行方法（均返回字典）
    def _run_single_tool(self, tool_name, host, panel, pause_event, params=None,
                         display_params=None, round_num=1, task_name="default",
                         exec_index=1, timeout=None):
        all_tools = self.tool_registry.get_all_tools()
        if tool_name not in all_tools:
            return {"success": False, "error": f"工具 {tool_name} 未找到"}

        params = params if isinstance(params, dict) else {}
        arg_names = self.tool_registry.args_of(tool_name)
        accepts_config = "--config" in arg_names
        # 日志永远用脱敏副本（在凭证解析之前生成的那一份）
        safe_params = display_params if display_params is not None else target_api.redact(params)

        if hasattr(self.app, 'structured_logger'):
            self.app.structured_logger.log_tool_start(host=host, tool=tool_name, args=safe_params)

        callbacks = _ToolCallbacks(self, host, panel, tool_name, round_num, task_name, exec_index)
        panel.append_log(f"🚀 启动 {tool_name} ...")
        self.app.log_target_program(host, f"🚀 启动 {tool_name} ...")

        # 参数通道：--config 优先；否则按同名命令行参数映射；未识别的键明确告警
        if accepts_config:
            if safe_params:
                shown = json.dumps(safe_params, ensure_ascii=False)
                panel.append_log(f"⚙️ 参数: {shown}")
                self.app.log_target_program(host, f"⚙️ {tool_name} 参数: {shown}")
        elif params:
            mapped = [k for k in params
                      if ("--" + str(k).strip().replace("_", "-")) in arg_names]
            unmapped = [k for k in params
                        if k not in mapped and k not in PLATFORM_PARAM_KEYS]
            if mapped:
                panel.append_log(f"⚙️ 参数（同名映射）: {', '.join('--' + str(k) for k in mapped)}")
            if unmapped:
                panel.append_log(f"[WARN] {tool_name} 不认识这些参数键，已忽略：{', '.join(unmapped)}")

        self.task_executor.submit(
            target=host, tool_name=tool_name, project_dir=self.app.project_dir,
            callbacks=callbacks, params=params, pause_event=pause_event,
            timeout=timeout, arg_names=arg_names,
        )
        result = callbacks.wait_for_completion()
        payload = _extract_json_result(result.get("output_lines") or [])
        if isinstance(payload, dict):
            result["result"] = payload
        return result

    def _run_freedom_atomic(self, tool_name, host, params, panel, pause_event,
                            round_num=1, task_name="default", exec_index=1,
                            timeout=None, display_params=None):
        tool_path = self.tool_registry.get_all_tools().get(tool_name)
        if not tool_path:
            return {"success": False, "error": f"工具 {tool_name} 未找到"}
        if not isinstance(params, dict): params = {}
        safe_params = display_params if display_params is not None else target_api.redact(params)
        params = dict(params)
        params["round"] = round_num
        params["task_name"] = task_name
        safe_params = dict(safe_params) if isinstance(safe_params, dict) else {}
        safe_params.setdefault("round", round_num)
        safe_params.setdefault("task_name", task_name)
        if hasattr(self.app, 'structured_logger'):
            self.app.structured_logger.log_tool_start(host=host, tool=tool_name, args=safe_params)
        while pause_event.is_set(): time.sleep(0.2)

        proc_timeout = timeout or (float(params.get("timeout", 60)) + 30)
        cmd = [sys.executable, tool_path, "--project-dir", str(self.app.project_dir),
               "--target", host, "--host", host,
               "--config", json.dumps(params, ensure_ascii=False)]
        # 日志只展示脱敏后的配置，避免凭证进 UI / 日志 / 诊断信息
        display = (f"{Path(tool_path).name} --target {host} "
                   f"--config {json.dumps(safe_params, ensure_ascii=False)}")

        panel.append_log(f"⚡ [编排] 执行: {display}")
        self.app.log_target_program(host, f"⚡ [编排] {tool_name} 开始执行...")
        panel.append_log(f"⚙️ [编排] 传入配置: {json.dumps(safe_params, ensure_ascii=False)}")
        self.app.log_target_program(host, f"⚙️ [编排] {tool_name} 配置: {json.dumps(safe_params, ensure_ascii=False)}")

        start_time = time.time()
        output_lines = []
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=proc_timeout, env=child_env({"PYTHONUNBUFFERED": "1"}))
            stderr_lines = proc.stderr.splitlines()
            for line in stderr_lines:
                clean_line = line.rstrip()
                # 限制单行输出长度，防止大页面输出导致 UI 卡死
                max_len = 300
                if len(clean_line) > max_len:
                    clean_line = clean_line[:max_len] + f"... [截断，原长度 {len(line)}]"
                panel.append_log(f"[{tool_name}] {clean_line}")
                self.app.log_target_program(host, f"[{tool_name}] {clean_line}")
                if hasattr(self.app, 'structured_logger'):
                    self.app.structured_logger.log_tool_output(host=host, tool=tool_name, line=clean_line)
                output_lines.append(clean_line)

            stdout = proc.stdout
            start_marker = "__JSON_RESULT_START__"
            end_marker = "__JSON_RESULT_END__"
            if start_marker in stdout and end_marker in stdout:
                json_str = stdout.split(start_marker)[1].split(end_marker)[0].strip()
                result = json.loads(json_str)
            else:
                result = {}
            duration = time.time() - start_time
            exit_code = proc.returncode
            status = "success" if exit_code == 0 else "failed"

            result.setdefault("host", host)
            result.setdefault("tool", tool_name)
            result.setdefault("exit_code", exit_code)
            result.setdefault("elapsed", round(duration, 2))
            result.setdefault("round", round_num)
            result.setdefault("task_name", task_name)
            result.setdefault("exec_index", exec_index)
            result.setdefault("output_lines", output_lines)

            panel.append_log(f"📋 [编排] 最终执行命令: {result.get('command', 'N/A')}")
            self.app.log_target_program(host, f"📋 [编排] {tool_name} 命令: {result.get('command', 'N/A')}")
            panel.append_log(f"📊 [编排] 返回码: {exit_code}, 耗时: {round(duration, 2)}s")
            self.app.log_target_program(host, f"📊 [编排] {tool_name} 返回码: {exit_code}, 耗时: {round(duration, 2)}s")

            if result.get("stdout"):
                out_preview = result["stdout"][:300] + ("..." if len(result["stdout"]) > 300 else "")
                panel.append_log(f"📤 [编排] 响应体:\n{out_preview}")
                self.app.log_target_program(host, f"📤 [编排] {tool_name} 响应: {out_preview}")
            if result.get("stderr"):
                err_preview = result["stderr"][:300]
                panel.append_log(f"📤 [编排] 错误/调试信息:\n{err_preview}")
                self.app.log_target_program(host, f"📤 [编排] {tool_name} stderr: {err_preview}")

            if hasattr(self.app, 'structured_logger'):
                self.app.structured_logger.log_tool_end(
                    host=host, tool=tool_name, status=status, duration=duration,
                    result_summary=result.get("stdout", "")[:200]
                )

            self.app.store_tool_output(host, tool_name, output_lines)
            self.app.log_target(host, f"✅ {tool_name} 执行完成 (exit={exit_code})")
            self._save_freedom_latest(host, tool_name, status, duration, output_lines, round_num, task_name, exec_index)
            return result

        except subprocess.TimeoutExpired:
            panel.append_log(f"[{tool_name}] ⏱ 执行超时")
            return {"success": False, "error": "超时", "exec_index": exec_index, "output_lines": output_lines}
        except json.JSONDecodeError:
            panel.append_log(f"[{tool_name}] ❌ JSON 结果解析失败")
            return {"success": False, "error": "JSON 解析失败", "exec_index": exec_index, "output_lines": output_lines}
        except Exception as e:
            panel.append_log(f"[{tool_name}] ❌ 执行异常: {str(e)}")
            return {"success": False, "error": str(e), "exec_index": exec_index, "output_lines": output_lines}

    def _run_skill(self, skill_name, host, params, panel, pause_event, round_num=1, task_name="default", exec_index=1):
        skill = self.app.orchestrator.skills.get(skill_name)
        if not skill:
            return {"success": False, "error": f"Skill {skill_name} 未注册"}

        # 套娃模式：三级 Skill 且参数中包含 phase3_task
        if "phase3" in skill.__class__.__module__ and "phase3_task" in params:
            phase3_task_name = params["phase3_task"]
            panel.append_log(f"[{skill_name}] 进入套娃模式，等待面板 [{phase3_task_name}] 完成...")
            result = skill.execute(
                app=self.app,
                host=host,
                task_name=phase3_task_name,  # 传入三级面板名作为阻塞 key
                blocking_mode=True,
                log_fn=lambda msg: panel.append_log(f"[{skill_name}] {msg}")
            )
            panel.append_log(f"[{skill_name}] 套娃结束，结果：{str(result)[:200]}")
            return {"success": True, "output": str(result), "exec_index": exec_index}

        skill.current_host = host
        if hasattr(self.app, 'structured_logger'):
            self.app.structured_logger.log_skill_event(host=host, event_type="start", skill=skill_name)
        original_log = skill.log_to_subpage

        def dual_log(h, msg):
            panel.append_log(f"[{skill_name}] {msg}")
            original_log(h, msg)
        skill.log_to_subpage = dual_log

        # 覆盖 _save_latest 写入带序号的轮次文件
        original_save_latest = skill._save_latest
        def round_save_latest(h, status):
            project_dir = getattr(self.app, 'project_dir', None)
            if not project_dir: return
            # 运行中节流 300ms，终态立即写盘
            now = time.time()
            if status == "running" and (now - getattr(skill, '_last_save', 0.0)) < 0.3:
                return
            skill._last_save = now
            host_safe = target_api.slug(h)
            round_dir = Path(project_dir) / "latest" / host_safe / f"round_{round_num}"
            round_dir.mkdir(parents=True, exist_ok=True)
            latest_file = round_dir / f"{skill_name}_{exec_index}.json"
            data = {
                "skill": skill_name, "host": h, "status": status, "round": round_num,
                "task_name": task_name, "exec_index": exec_index,
                "start_time": (
                    datetime.fromtimestamp(skill.start_time).isoformat(timespec="seconds")
                    if skill.start_time
                    else datetime.now().isoformat(timespec="seconds")
                ),
                "last_update": datetime.now().isoformat(timespec='seconds'),
                "full_output": skill._log_buffer.copy()
            }
            try:
                with open(latest_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass  # 快照写失败不阻断执行
        skill._save_latest = lambda h, status: round_save_latest(h, status)

        try:
            while pause_event.is_set(): time.sleep(0.2)
            panel.append_log(f"🚀 启动 Skill {skill_name} ...")
            self.app.log_target_program(host, f"🚀 启动 Skill {skill_name} ...")
            result = skill.execute(**params)
            if isinstance(result, tuple) and len(result) >= 2 and result[0] == "__WAIT__":
                panel.append_log("[ERROR] 该技能需要交互输入，当前版本不支持")
                return {"success": False, "error": "需要交互输入", "exec_index": exec_index}
            self._wait_skill_complete(host, skill_name, timeout=300, pause_event=pause_event,
                                      round_num=round_num, exec_index=exec_index)
            latest_data = self._read_skill_latest(host, skill_name, round_num, exec_index)
            status = latest_data.get('status', 'unknown')
            full_output = latest_data.get("full_output", [])
            for item in full_output:
                line = item.get("line", "") if isinstance(item, dict) else str(item)
                if line: panel.append_log(f"[{skill_name}] {line}")
            panel.append_log(f"[SUCCESS] {skill_name} 完成，状态: {status}")
            self.app.log_target(host, f"✅ {skill_name} 执行完成")
            latest_data["success"] = (status == "success")
            latest_data["exec_index"] = exec_index
            return latest_data
        except Exception as e:
            panel.append_log(f"[{skill_name}] ❌ 执行失败: {str(e)}")
            return {"success": False, "error": str(e), "exec_index": exec_index}
        finally:
            skill.log_to_subpage = original_log
            skill._save_latest = original_save_latest

    def _wait_skill_complete(self, host, skill_name, timeout=300, pause_event=None,
                             round_num=1, exec_index=1):
        host_safe = target_api.slug(host)
        project_dir = self.app.project_dir
        if not project_dir: raise Exception("项目目录未设置")
        # 按 exec_index 监视本次执行的快照，避免同一技能第二次执行时读到旧终态
        latest_file = Path(project_dir) / "latest" / host_safe / f"round_{round_num}" / f"{skill_name}_{exec_index}.json"
        start = time.time()
        while time.time() - start < timeout:
            if pause_event and pause_event.is_set():
                time.sleep(0.5)
                continue
            if latest_file.exists():
                try:
                    with open(latest_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if data.get("status") in ("success", "failed"):
                        return data
                except Exception:
                    pass  # 尚未写成终态，继续轮询
            time.sleep(0.5)
        raise TimeoutError(f"Skill {skill_name} 超时未完成")

    def _read_skill_latest(self, host, skill_name, round_num, exec_index=1):
        host_safe = target_api.slug(host)
        latest_file = (Path(self.app.project_dir) / "latest" / host_safe
                       / f"round_{round_num}" / f"{skill_name}_{exec_index}.json")
        if latest_file.exists():
            with open(latest_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def _save_freedom_latest(self, host, tool_name, status, duration, output_lines,
                             round_num=1, task_name="default", exec_index=1):
        project_dir = getattr(self.app, 'project_dir', None)
        if not project_dir: return
        host_safe = target_api.slug(host)
        round_dir = Path(project_dir) / "latest" / host_safe / f"round_{round_num}"
        round_dir.mkdir(parents=True, exist_ok=True)
        latest_file = round_dir / f"{tool_name}_{exec_index}.json"
        data = {
            "tool": tool_name, "host": host, "status": status, "round": round_num,
            "task_name": task_name, "exec_index": exec_index,
            "start_time": datetime.now().isoformat(timespec='seconds'),
            "end_time": datetime.now().isoformat(timespec='seconds'),
            "duration": round(duration, 2),
            "output_lines": output_lines.copy()
        }
        try:
            with open(latest_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 快照写失败不阻断执行

    def _step_timeout(self, params):
        """单步进程超时。

        params["timeout"] 表示"工具自身的超时"，进程超时给它留 30s 宽限；
        未声明时使用 config.tools.DEFAULT_STEP_TIMEOUT（默认 900s = 15 分钟）。
        """
        base = params.get("timeout") if isinstance(params, dict) else None
        if base is None:
            return DEFAULT_STEP_TIMEOUT
        try:
            return max(1.0, float(base) + 30)
        except (TypeError, ValueError):
            return DEFAULT_STEP_TIMEOUT

    def _parse_parameters(self, task_id, raw_params, inherit_from=None, inherit_file=None):
        try:
            raw = json.loads(raw_params) if isinstance(raw_params, str) else (raw_params or {})
            params = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            params = {}
        if inherit_file:
            try:
                with open(inherit_file, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                params["inherit_data"] = file_data
            except Exception as e:
                params["inherit_data"] = f"[继承文件读取失败: {e}]"
        elif inherit_from and inherit_from != "无" and task_id in self.task_data:
            inherited_data = self.task_data[task_id].get(inherit_from, "")
            params["inherit_data"] = inherited_data
        return params
