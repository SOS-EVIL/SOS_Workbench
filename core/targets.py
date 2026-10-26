"""core/targets.py - target 抽象。

平台把 target 当作不透明字符串，只做四件事：
    parse()            拆出可选 kind 前缀
    slug()             生成安全目录名
    alias()            生成显示名
    resolve_secrets()  解析 {"$secret": "ENV_NAME"}

约束：纯域名/IP 的 slug 必须与旧实现 host.replace('.', '_') 逐字符一致，
否则历史目录会失联。
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import urlparse

__all__ = [
    "parse", "kind_of", "slug", "alias", "normalize_input",
    "resolve_secrets", "redact", "has_secret", "SecretNotFound", "SECRET_KEY",
]

SECRET_KEY = "$secret"

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+._-]*)://")
_KIND_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_+-]{1,15}):(?!//)")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_LEGACY_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

_MAX_SLUG_BODY = 60
_MAX_ALIAS = 42

class SecretNotFound(KeyError):
    pass

def parse(target: str) -> tuple[str, str]:
    """返回 (kind, value)。kind 为空表示未显式标注。"""
    t = (target or "").strip()
    if not t:
        return "", ""

    m = _SCHEME_RE.match(t)
    if m:
        return m.group(1).lower(), t

    if not _DRIVE_RE.match(t):
        m = _KIND_RE.match(t)
        if m:
            return m.group(1).lower(), t[m.end():].strip()

    return "", t

def kind_of(target: str) -> str:
    return parse(target)[0]

def slug(target: str) -> str:
    t = (target or "").strip()
    if not t:
        return "unknown"

    if _LEGACY_SAFE_RE.match(t):
        return t.replace(".", "_")

    kind, value = parse(t)
    digest = hashlib.sha1(f"{kind}:{value}".encode("utf-8")).hexdigest()[:8]
    body = _UNSAFE_RE.sub("_", value or t).strip("._-")[:_MAX_SLUG_BODY]
    return f"{body or 'target'}__{digest}"

def alias(target: str, limit: int = _MAX_ALIAS) -> str:
    t = " ".join((target or "").split())
    if not t:
        return "(空)"
    return t if len(t) <= limit else t[: max(1, limit - 1)] + "…"

def normalize_input(raw: str) -> str:
    """把用户输入变成平台内部的 target。纯域名保持旧行为（取 netloc）。"""
    s = (raw or "").strip()
    if not s:
        return ""

    if _DRIVE_RE.match(s):
        return s
    m = _SCHEME_RE.match(s)
    if m:
        return s if m.group(1).lower() not in ("http", "https") else (urlparse(s).netloc or s)
    if _KIND_RE.match(s):
        return s

    probe = s if s.lower().startswith("http") else "https://" + s
    try:
        netloc = urlparse(probe).netloc
    except Exception:
        netloc = ""
    return netloc or s

def has_secret(obj) -> bool:
    if isinstance(obj, dict):
        if set(obj) == {SECRET_KEY}:
            return True
        return any(has_secret(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_secret(v) for v in obj)
    return False

def resolve_secrets(obj, env=None):
    """把 {"$secret": "NAME"} 换成环境变量值，返回新对象。"""
    env = os.environ if env is None else env

    if isinstance(obj, dict):
        if set(obj) == {SECRET_KEY}:
            name = str(obj[SECRET_KEY]).strip()
            if not name:
                raise SecretNotFound("$secret 的变量名为空")
            if name not in env:
                raise SecretNotFound(f'未找到环境变量 {name}（由 {{"$secret": "{name}"}} 引用）')
            return env[name]
        return {k: resolve_secrets(v, env) for k, v in obj.items()}

    if isinstance(obj, list):
        return [resolve_secrets(v, env) for v in obj]

    return obj

def redact(obj):
    """日志/快照用：凭证替换为 ***。"""
    if isinstance(obj, dict):
        if set(obj) == {SECRET_KEY}:
            return {SECRET_KEY: obj[SECRET_KEY], "value": "***"}
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj
