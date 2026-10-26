#!/usr/bin/env python3
"""http_probe.py - 示例工具：只读 HTTP 探测（GET / HEAD）。

target：https://host/path 或裸域名（无协议时按 https）
params：{"method": "GET", "path": "/health", "headers": {}, "timeout": 10, "max_bytes": 65536}

安全边界：
  · 只允许 GET / HEAD，不接受自定义命令串
  · 只允许 http / https
  · 响应体截断，不写文件

契约：日志走 stderr，结果走 stdout 的 __JSON_RESULT_START__ / __JSON_RESULT_END__。
"""

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ALLOWED_METHODS = ("GET", "HEAD")
MAX_BODY_CAP = 1024 * 1024
DEFAULT_MAX_BYTES = 64 * 1024


def log(msg):
    print(f"  {msg}", file=sys.stderr)
    sys.stderr.flush()


def emit(payload):
    print("__JSON_RESULT_START__")
    print(json.dumps(payload, ensure_ascii=False, default=str))
    print("__JSON_RESULT_END__")
    sys.stdout.flush()


def build_url(target, path):
    t = (target or "").strip()
    for prefix in ("url:", "http_target:"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):].strip()
    if not t.lower().startswith(("http://", "https://")):
        t = "https://" + t
    if path:
        t = urljoin(t if t.endswith("/") else t + "/", path.lstrip("/"))
    return t


def clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--config", default="{}")
    args = parser.parse_args()

    target = (args.target or args.host or "").strip()
    try:
        config = json.loads(args.config or "{}")
    except json.JSONDecodeError:
        config = {}
    if not isinstance(config, dict):
        config = {}

    method = str(config.get("method", "GET")).upper()
    timeout = clamp(config.get("timeout", 10), 1, 60, 10)
    max_bytes = clamp(config.get("max_bytes", DEFAULT_MAX_BYTES), 1024, MAX_BODY_CAP, DEFAULT_MAX_BYTES)
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    headers.setdefault("User-Agent", "SOS_Workbench/0.1.0")

    if not target:
        log("[ERROR] target 为空")
        print("__ERROR__:target 为空")
        emit({"success": False, "error": "target 为空"})
        return 1

    if method not in ALLOWED_METHODS:
        msg = f"只允许 GET/HEAD，收到 {method}"
        log(f"[ERROR] {msg}")
        print(f"__ERROR__:{msg}")
        emit({"success": False, "error": msg})
        return 2

    try:
        url = build_url(target, config.get("path", ""))
    except Exception as e:
        log(f"[ERROR] URL 构造失败: {e}")
        print(f"__ERROR__:{e}")
        emit({"success": False, "error": str(e)})
        return 1

    log(f"[http_probe] {method} {url}")

    start = time.time()
    try:
        req = Request(url, headers=headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            raw = b"" if method == "HEAD" else resp.read(max_bytes)
            elapsed = round(time.time() - start, 3)
            log(f"[http_probe] 状态 {resp.status}，读取 {len(raw)} 字节，耗时 {elapsed}s")
            emit({
                "success": True,
                "tool": "http_probe",
                "target": target,
                "url": url,
                "method": method,
                "status": resp.status,
                "content_type": resp.headers.get("Content-Type", ""),
                "body_bytes": len(raw),
                "truncated": len(raw) >= max_bytes,
                "body_preview": raw.decode("utf-8", errors="replace")[:4000],
                "elapsed": elapsed,
            })
            return 0

    except HTTPError as e:
        elapsed = round(time.time() - start, 3)
        log(f"[http_probe] HTTP {e.code} {e.reason}")
        emit({
            "success": True,
            "tool": "http_probe",
            "target": target,
            "url": url,
            "method": method,
            "status": e.code,
            "reason": str(e.reason),
            "elapsed": elapsed,
        })
        return 0

    except (URLError, TimeoutError, OSError) as e:
        elapsed = round(time.time() - start, 3)
        log(f"[ERROR] 请求失败: {e}")
        print(f"__ERROR__:{type(e).__name__}: {e}")
        emit({"success": False, "target": target, "url": url, "error": str(e), "elapsed": elapsed})
        return 1


if __name__ == "__main__":
    sys.exit(main())
