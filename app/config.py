"""配置加载：默认值 ← config/*.yaml（占位）← 环境变量/.env（真实值覆盖）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_settings(config_dir: str | Path = "config") -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    config_dir = ROOT / config_dir
    app = yaml.safe_load((config_dir / "app.yaml").read_text(encoding="utf-8"))
    models = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((config_dir / "scan_rules.yaml").read_text(encoding="utf-8"))

    # *.local.yaml 内网覆盖（可选，gitignore）
    local = config_dir / "app.local.yaml"
    if local.exists():
        _deep_merge(app, yaml.safe_load(local.read_text(encoding="utf-8")))

    return {
        "app": app,
        "models": models,
        "scan_rules": rules,
        "mcp_config_path": str(config_dir / "mcp_config.json"),
    }


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}，请检查 .env")
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
