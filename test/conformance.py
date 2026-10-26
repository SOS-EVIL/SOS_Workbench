"""
test/conformance.py - 路线 A 契约一致性测试（无 GUI、无网络）

覆盖：
  1. target 解析 / slug 向后兼容 / 非法字符 / 唯一性
  2. 用户输入归一化
  3. 凭证占位符解析与日志脱敏
  4. 工具参数能力探测（--target / --config 新旧并存）
  5. ToolRunner：流式日志、JSON 结果、超时进程树强杀、取消
  6. TaskExecutor：真并发上限、按 target 精确 kill
  7. session.log 写入器
  8. http_probe 只读示例
  9. 子进程环境隔离

运行： python test/conformance.py
"""

import json
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import targets as tg                      # noqa: E402
from core.tool_runner import ToolRunner, child_env   # noqa: E402
from core.task_executor import TaskExecutor          # noqa: E402
from core.tool_registry import ToolRegistry          # noqa: E402
from config.tools import DEFAULT_STEP_TIMEOUT        # noqa: E402

RESULTS = []

def check(name, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   << " + str(detail)) if (detail and not ok) else ""))

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out

def run_runner(tool_path, target, project_dir, params=None, timeout=None,
               arg_names=None, cancel_after=None):
    runner = ToolRunner(str(tool_path), target, project_dir, params=params,
                        timeout=timeout, arg_names=arg_names,
                        tool_name=Path(tool_path).stem)
    q = queue.Queue()
    cancel = threading.Event()
    if cancel_after:
        threading.Timer(cancel_after, cancel.set).start()
    t0 = time.time()
    runner.run(q, None, cancel)
    return drain(q), time.time() - t0

def parse_json_block(lines):
    try:
        s = lines.index("__JSON_RESULT_START__")
        e = lines.index("__JSON_RESULT_END__")
    except ValueError:
        return None
    try:
        return json.loads("\n".join(lines[s + 1:e]))
    except json.JSONDecodeError:
        return None

SLEEPER = '''
import argparse, json, sys, time
p = argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--target", default="")
p.add_argument("--host", default="")
p.add_argument("--config", default="{}")
a = p.parse_args()
cfg = json.loads(a.config or "{}")
n = float(cfg.get("sleep", 1))
print("sleeper start t=%s" % a.target, file=sys.stderr, flush=True)
time.sleep(n)
print("sleeper done", file=sys.stderr, flush=True)
print("__JSON_RESULT_START__")
print(json.dumps({"success": True, "slept": n}))
print("__JSON_RESULT_END__")
'''

LEGACY = '''
import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--host", default="")
a = p.parse_args()
print("legacy got host=%s" % a.host, file=sys.stderr, flush=True)
'''

MAPPED = '''
import argparse, sys
p = argparse.ArgumentParser()
p.add_argument("--project-dir", default="")
p.add_argument("--host", default="")
p.add_argument("--mode", default="quick")
a = p.parse_args()
print("mapped mode=%s host=%s" % (a.mode, a.host), file=sys.stderr, flush=True)
'''

BAT = '''@echo off
echo hello args: %* 1>&2
echo __JSON_RESULT_START__
echo {"success": true}
echo __JSON_RESULT_END__
'''

def main():
    tmp = Path(tempfile.mkdtemp(prefix="sos_conf_"))
    try:
        # 1. target 解析
        print("\n[1] target 解析 / slug")
        check("scheme 识别", tg.parse("postgresql://u@h/db") == ("postgresql", "postgresql://u@h/db"),
              tg.parse("postgresql://u@h/db"))
        check("kind 前缀识别", tg.parse("db:foo") == ("db", "foo"), tg.parse("db:foo"))
        check("Windows 盘符不误判", tg.parse(r"D:\data\x.csv") == ("", r"D:\data\x.csv"),
              tg.parse(r"D:\data\x.csv"))
        check("纯域名无 kind", tg.parse("a.b.com") == ("", "a.b.com"))

        legacy_pairs = [
            ("WWW.BAIDU.COM", "WWW_BAIDU_COM"),
            ("m1.pxb7.com", "m1_pxb7_com"),
            ("example.com", "example_com"),
            ("10.0.0.5", "10_0_0_5"),
        ]
        for src, want in legacy_pairs:
            check(f"slug 向后兼容 {src}", tg.slug(src) == want, f"{tg.slug(src)} != {want}")

        dsn = "postgresql://user:pw@db.internal:5432/analytics"
        s = tg.slug(dsn)
        check("DSN slug 无非法字符", not any(c in s for c in '\\/:*?"<>|'), s)
        check("DSN slug 带哈希后缀", "__" in s and len(s) <= 80, s)
        check("db:x 与 file:x 不碰撞", tg.slug("db:x") != tg.slug("file:x"),
              (tg.slug("db:x"), tg.slug("file:x")))
        check("空 target 不崩", tg.slug("") == "unknown" and tg.slug(None) == "unknown")

        # 2. 输入归一化
        print("\n[2] 用户输入归一化")
        cases = [
            ("example.com", "example.com"),
            ("https://login.example.com/path", "login.example.com"),
            ("example.com:8080/a", "example.com:8080"),
            ("db:postgresql://u@h/db", "db:postgresql://u@h/db"),
            ("postgresql://u@h/db", "postgresql://u@h/db"),
            (r"D:\data\2026\*.csv", r"D:\data\2026\*.csv"),
        ]
        for raw, want in cases:
            got = tg.normalize_input(raw)
            check(f"归一化 {raw}", got == want, f"{got} != {want}")

        # 3. 凭证
        print("\n[3] 凭证占位符")
        env = {"DB_PASS": "s3cr3t"}
        params = {"dsn": "postgresql://u@h/db", "password": {"$secret": "DB_PASS"}}
        resolved = tg.resolve_secrets(params, env)
        check("凭证解析", resolved["password"] == "s3cr3t", resolved)
        check("原对象未被修改", params["password"] == {"$secret": "DB_PASS"}, params)
        red = tg.redact(params)          # 脱敏必须作用在**未解析**的原始参数上
        check("日志脱敏", red["password"]["value"] == "***" and "s3cr3t" not in json.dumps(red), red)
        try:
            tg.resolve_secrets(params, {})
            check("缺失凭证抛错", False, "没有抛异常")
        except tg.SecretNotFound as e:
            check("缺失凭证抛错", "DB_PASS" in str(e), str(e))

        # 4. 参数能力探测
        print("\n[4] 工具参数能力探测")
        tool_dir = tmp / "tools"
        tool_dir.mkdir()
        sleeper = tool_dir / "sleeper.py"
        sleeper.write_text(SLEEPER, encoding="utf-8")
        legacy = tool_dir / "legacy.py"
        legacy.write_text(LEGACY, encoding="utf-8")
        mapped = tool_dir / "mapped.py"
        mapped.write_text(MAPPED, encoding="utf-8")
        bat = tool_dir / "hello.bat"
        bat.write_text(BAT, encoding="utf-8")

        reg = ToolRegistry(tool_dirs=[tool_dir])
        check("新工具识别 --target/--config",
              reg.accepts_target("sleeper") and reg.accepts_config("sleeper"),
              sorted(reg.args_of("sleeper")))
        check("旧工具只有 --host",
              (not reg.accepts_target("legacy")) and (not reg.accepts_config("legacy"))
              and "--host" in reg.args_of("legacy"),
              sorted(reg.args_of("legacy")))

        # 5. ToolRunner
        print("\n[5] ToolRunner 执行 / 超时 / 取消")
        demo = ROOT / "external_tools" / "sqlite_query.py"
        db = tmp / "demo.db"
        conn = sqlite3.connect(db)
        conn.execute("create table t(id integer, name text)")
        conn.executemany("insert into t values(?,?)", [(1, "a"), (2, "b"), (3, "c")])
        conn.commit()
        conn.close()

        demo_args = frozenset({"--project-dir", "--target", "--config"})
        lines, dur = run_runner(demo, str(db), tmp,
                                params={"sql": "select * from t order by id", "limit": 10},
                                timeout=30, arg_names=demo_args)
        payload = parse_json_block(lines)
        check("非网页工具可执行（sqlite 文件当 target）",
              payload and payload.get("success") and payload.get("row_count") == 3, payload)
        check("流式日志到达", any("已以只读模式连接" in ln for ln in lines), lines[:5])
        check("旧字段兼容（--host 也在）", payload and payload.get("target") == str(db), payload)

        lines, _ = run_runner(demo, str(db), tmp,
                              params={"sql": "delete from t"}, timeout=30, arg_names=demo_args)
        check("只读白名单拦截写操作", any("只读模式拒绝执行" in ln for ln in lines), lines[:5])

        lines, dur = run_runner(sleeper, "t-timeout", tmp, params={"sleep": 30},
                                timeout=1.5, arg_names=frozenset({"--project-dir", "--target", "--config"}))
        check("超时强杀并返回错误", any("执行超时" in ln for ln in lines) and dur < 8, f"dur={dur:.1f}")
        check("超时不残留进程", not any("sleeper done" in ln for ln in lines), lines[-3:])

        lines, dur = run_runner(sleeper, "t-cancel", tmp, params={"sleep": 30},
                                timeout=60, cancel_after=0.6,
                                arg_names=frozenset({"--project-dir", "--target", "--config"}))
        check("取消生效", any("执行已取消" in ln for ln in lines) and dur < 8, f"dur={dur:.1f}")

        pause = threading.Event()
        pause.set()
        cancel = threading.Event()
        paused_runner = ToolRunner(str(sleeper), "t-pause", tmp, params={"sleep": 30},
                                   timeout=1.5,
                                   arg_names=frozenset({"--project-dir", "--target", "--config"}))
        pq = queue.Queue()
        pth = threading.Thread(target=lambda: paused_runner.run(pq, pause, cancel), daemon=True)
        pth.start()
        time.sleep(2.6)                      # 超过 timeout 两倍
        still_alive = paused_runner.is_running()
        cancel.set()
        pause.clear()
        pth.join(10)
        check("暂停期间不触发超时", still_alive, f"alive={still_alive}")
        check("解除暂停后仍可取消", not pth.is_alive())

        lines, _ = run_runner(legacy, "old-host.example.com", tmp,
                              arg_names=reg.args_of("legacy"))
        check("旧工具不会被塞 --target", any("legacy got host=old-host.example.com" in ln for ln in lines),
              lines[:5])

        lines, _ = run_runner(mapped, "mx.example.com", tmp,
                              params={"mode": "deep", "unknown_key": "zzz"},
                              arg_names=reg.args_of("mapped"))
        check("params 同名映射到旧工具参数（--mode）",
              any("mapped mode=deep" in ln for ln in lines), lines[:5])
        check("未声明的参数键被忽略而不是报错",
              not any("unrecognized" in ln.lower() for ln in lines), lines[:5])

        check(".bat 被识别为完整契约",
              "--target" in reg.args_of("hello") and "--config" in reg.args_of("hello"),
              sorted(reg.args_of("hello")))
        bat_lines, _ = run_runner(bat, "bat-target.example.com", tmp,
                                  params={"k": "v"}, arg_names=reg.args_of("hello"))
        check("非 Python 工具（.bat）收到 target 与 config",
              any("--target bat-target.example.com" in ln for ln in bat_lines)
              and any("--config" in ln for ln in bat_lines), bat_lines[:3])
        check(".bat 的 JSON 结果可解析",
              parse_json_block(bat_lines) == {"success": True}, bat_lines)

        # 6. TaskExecutor
        print("\n[6] TaskExecutor 并发与终止")
        check("默认超时为 900s", DEFAULT_STEP_TIMEOUT == 900, DEFAULT_STEP_TIMEOUT)

        def run_3(limit):
            ex = TaskExecutor(concurrency_getter=lambda: limit,
                              tool_resolver=lambda: {"sleeper": str(sleeper)},
                              default_timeout=60)
            args = frozenset({"--project-dir", "--target", "--config"})

            def job():
                ex.submit(target="t-conc", tool_name="sleeper", project_dir=tmp,
                          params={"sleep": 0.5}, arg_names=args)

            ths = [threading.Thread(target=job) for _ in range(3)]
            t0 = time.time()
            for t in ths:
                t.start()
            for t in ths:
                t.join(30)
            return time.time() - t0

        t2 = run_3(2)
        t3 = run_3(3)
        check("并发上限真实生效（3 任务：limit=2 明显慢于 limit=3）",
              t2 > t3 + 0.25 and t2 < 4 and t3 < 4, f"limit2={t2:.2f}s limit3={t3:.2f}s")

        ex = TaskExecutor(concurrency_getter=lambda: 3,
                          tool_resolver=lambda: {"sleeper": str(sleeper)},
                          default_timeout=60)
        args = frozenset({"--project-dir", "--target", "--config"})

        def long_job():
            ex.submit(target="killme", tool_name="sleeper", project_dir=tmp,
                      params={"sleep": 30}, arg_names=args)

        th = threading.Thread(target=long_job)
        th.start()
        time.sleep(1.0)
        killed = ex.kill_by_target("killme")
        t0 = time.time()
        th.join(10)
        dt = time.time() - t0
        check("按 target 精确终止", killed == 1 and not th.is_alive() and dt < 8,
              f"killed={killed} alive={th.is_alive()} dt={dt:.1f}")

        # 7. 文本日志写入器
        print("\n[7] session.log 写入器（历史 bug 回归）")
        from core.log_writer import LogWriter
        logdir = tmp / "proj_log"
        logdir.mkdir()
        lw = LogWriter(lambda: logdir, flush_interval=0.2)
        lw.write("line-1\n")
        lw.write("line-2\n")
        time.sleep(0.6)
        lw.close()
        logfile = logdir / "session.log"
        check("异步落盘生效", logfile.exists() and "line-2" in logfile.read_text(encoding="utf-8"),
              str(logfile))
        check("close 后缓冲已清空", lw.pending() == 0, lw.pending())
        lw2 = LogWriter(lambda: None, flush_interval=0.1)
        lw2.write("x\n")
        check("未设置项目时 flush 安全返回 0", lw2.flush() == 0)
        lw2.close()

        # 8. 示例工具 http_probe（本地回环，不出网）
        print("\n[8] http_probe 只读 HTTP 示例")
        import http.server
        import socketserver

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>probe-ok</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        http_tool = ROOT / "external_tools" / "http_probe.py"
        reg2 = ToolRegistry()
        http_args = reg2.args_of("http_probe")
        server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            lines, _ = run_runner(http_tool, f"http://127.0.0.1:{port}/", tmp,
                                  params={"method": "GET"}, arg_names=http_args)
            payload = parse_json_block(lines)
            check("http_probe 读取 GET 响应",
                  payload and payload.get("success") and payload.get("status") == 200
                  and "probe-ok" in payload.get("body_preview", ""), payload)

            lines, _ = run_runner(http_tool, f"http://127.0.0.1:{port}/", tmp,
                                  params={"method": "POST"}, arg_names=http_args)
            check("http_probe 拒绝非 GET/HEAD",
                  any("只允许 GET/HEAD" in ln for ln in lines), lines[:4])
        finally:
            server.shutdown()
            server.server_close()

        # 9. 子进程环境隔离
        print("\n[9] 子进程环境隔离")
        old_key = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "test-secret"
        try:
            env = child_env({"TARGET": "demo", "PROJECT_DIR": str(tmp)})
            check("child_env 移除平台 Key", "DEEPSEEK_API_KEY" not in env)
            check("child_env 保留显式变量", env.get("TARGET") == "demo")
        finally:
            if old_key is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = old_key

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 58)
    print(f"结果：{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}   {detail}")
    print("=" * 58)
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
