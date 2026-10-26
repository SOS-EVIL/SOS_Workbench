# !/usr/bin/env python3
"""
sqlite_query.py - 示例工具：**只读 SQLite 查询**（非网页领域）

它的存在只为一件事：证明平台对 target 不做任何假设。

    target = 任意 .db 文件路径（也接受 file: / sqlite: / db: 前缀）
    params = {"sql": "select ...", "limit": 50}

平台不认识 SQL、不认识数据库，它只做：
    传 target + params  →  收 stderr 日志  →  收 stdout 的 JSON 结果

契约：
    --project-dir  平台约定（必填）
    --target       路线 A 新契约：目标（不透明字符串）
    --host         旧契约别名（过渡兼容）
    --config       JSON 参数

日志 → stderr 逐行；结构化结果 → stdout 的
    __JSON_RESULT_START__ {json} __JSON_RESULT_END__

安全：只读模式打开（mode=ro），且只允许 SELECT / WITH / EXPLAIN。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

def log(msg):
    print(f"  {msg}", file=sys.stderr)
    sys.stderr.flush()

def emit(result: dict):
    print("__JSON_RESULT_START__")
    print(json.dumps(result, ensure_ascii=False, default=str))
    print("__JSON_RESULT_END__")
    sys.stdout.flush()

# 只读语句白名单（示例级防护，真实工具应更严格）
_ALLOWED = ("select", "with", "explain")

def _first_keyword(sql: str) -> str:
    s = sql.strip().lstrip("(").strip()
    return s.split(None, 1)[0].lower() if s else ""

def strip_kind(target: str) -> str:
    """去掉可选的 kind 前缀：file:/sqlite:/db:"""
    for prefix in ("file:", "sqlite:", "db:"):
        if target.lower().startswith(prefix) and "://" not in target[: len(prefix) + 3]:
            return target[len(prefix):]
    return target

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--target", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--config", default="{}")
    args = parser.parse_args()

    target = (args.target or args.host or "").strip()
    try:
        config = json.loads(args.config) if args.config else {}
    except json.JSONDecodeError:
        config = {}

    sql = str(config.get("sql", "")).strip()
    limit = int(config.get("limit", 50) or 50)

    log(f"[sqlite_query] target = {target}")
    log(f"[sqlite_query] project_dir = {args.project_dir}")

    if not target:
        print("__ERROR__:target 为空", file=sys.stdout)
        emit({"success": False, "error": "target 为空"})
        return 1

    db_path = strip_kind(target)
    if not os.path.isfile(db_path):
        msg = f"数据库文件不存在: {db_path}"
        log(f"[ERROR] {msg}")
        print(f"__ERROR__:{msg}", file=sys.stdout)
        emit({"success": False, "target": db_path, "error": msg})
        return 1

    if not sql:
        msg = "params 缺少 sql"
        log(f"[ERROR] {msg}")
        print(f"__ERROR__:{msg}", file=sys.stdout)
        emit({"success": False, "target": db_path, "error": msg})
        return 1

    kw = _first_keyword(sql)
    if kw not in _ALLOWED:
        msg = f"只读模式拒绝执行：{kw}"
        log(f"[ERROR] {msg}")
        print(f"__ERROR__:{msg}", file=sys.stdout)
        emit({"success": False, "target": db_path, "error": msg})
        return 2

    start = time.time()
    try:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        log(f"[sqlite_query] 已以只读模式连接")
        log(f"[sqlite_query] 执行 SQL: {sql}")

        cur = conn.execute(sql)
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = [d[0] for d in (cur.description or [])]
        conn.close()

        log(f"[sqlite_query] 返回 {len(rows)} 行{'（已截断）' if truncated else ''}")
        emit({
            "success": True,
            "tool": "sqlite_query",
            "target": db_path,
            "sql": sql,
            "columns": columns,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed": round(time.time() - start, 3),
        })
        return 0

    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        log(f"[ERROR] {msg}")
        print(f"__ERROR__:{msg}", file=sys.stdout)
        emit({"success": False, "target": db_path, "error": msg,
              "elapsed": round(time.time() - start, 3)})
        return 1

if __name__ == "__main__":
    sys.exit(main())
