"""配置与密钥读取。

第三方密钥放在仓库外的 secrets.env（只读挂载），每次调用时重新读取，
所以你改完密钥不用重启任何东西。
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
APP_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", APP_DIR / "prompts"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", APP_DIR / "config"))
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", "/data/assets"))
SECRETS_FILE = Path(os.environ.get("SECRETS_FILE", "/secrets/secrets.env"))
RSSHUB_URL = os.environ.get("RSSHUB_URL", "http://rsshub:1200").rstrip("/")


def database_url() -> str:
    return os.environ["DATABASE_URL"]


def load_secrets(path: Path | None = None) -> dict[str, str]:
    path = path or SECRETS_FILE
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            out[k.strip()] = v
    return out


def secret(name: str, default: str = "") -> str:
    return load_secrets().get(name) or os.environ.get(name, default)


def flag(name: str) -> bool:
    return secret(name, "").lower() in {"1", "true", "yes", "on"}


def secrets_status() -> dict[str, bool]:
    s = load_secrets()
    keys = ["DEEPSEEK_API_KEY", "TDOCS_MCP_TOKEN", "MOWEN_API_KEY", "LIVEPAGE_TOKEN"]
    return {k: bool(s.get(k)) for k in keys}
