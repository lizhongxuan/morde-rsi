"""Service configuration (stdlib only).

Loads ``service/config.json`` (optional) over DEFAULTS. Secrets are never
stored here: the HTTP ``api_token`` falls back to the ``RSI_API_TOKEN``
environment variable, the fixer API key comes from the env var named by
``fixer.api_key_env`` (default ``DEEPSEEK_API_KEY``), and the GitHub token
from the env var named by ``github.token_env`` (default ``GITHUB_TOKEN``).
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8765,
    "data_dir": "runs/service",
    "api_token": None,
    "fixer": {
        "provider": "openai-compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout_s": 120,
        "max_tokens": 8192,
        "price_per_1m_input_usd": None,
        "price_per_1m_output_usd": None,
    },
    "budgets": {
        "max_submissions": 3,
        "time_budget_s": 3600,
        "max_usd_per_job": None,
    },
    "github": {
        "create_pr": False,
        "token_env": "GITHUB_TOKEN",
    },
    "domains": {
        "morde": {"repo": "/home/hatch/workspace/morde-check"},
        "self": {"repo": "/home/hatch/workspace/morde-rsi"},
    },
}

_KNOWN_DOMAINS = frozenset({"morde", "self"})


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` over ``base`` (override wins)."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path=None) -> dict:
    """Load config, deep-merging ``path`` over DEFAULTS.

    Default path is ``<repo_root>/service/config.json``. A missing file
    means all defaults. Keys starting with ``_`` (e.g. ``_comment`` in
    ``config.example.json``) are not configuration and are dropped. If
    ``api_token`` is empty/None after merging, it falls back to the
    ``RSI_API_TOKEN`` environment variable.
    """
    cfg = copy.deepcopy(DEFAULTS)
    if path is None:
        path = REPO_ROOT / "service" / "config.json"
    path = Path(path)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"config file must contain a JSON object: {path}")
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        cfg = _deep_merge(cfg, data)
    token = cfg.get("api_token")
    if not token:
        env_token = os.environ.get("RSI_API_TOKEN")
        cfg["api_token"] = env_token if env_token else None
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Validate ``cfg``.

    Returns a list of warnings (never raises for warnings). Raises
    ValueError on hard errors: bad port, negative budgets/timeout/
    max_tokens, or a malformed/unknown ``domains`` mapping.
    """
    warnings: list[str] = []

    port = cfg.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"port must be an int in 1..65535, got {port!r}")

    fixer = cfg.get("fixer") or {}
    budgets = cfg.get("budgets") or {}
    if not isinstance(fixer, dict):
        raise ValueError("fixer must be an object")
    if not isinstance(budgets, dict):
        raise ValueError("budgets must be an object")
    for section, key in (
        (fixer, "timeout_s"),
        (fixer, "max_tokens"),
        (budgets, "max_submissions"),
        (budgets, "time_budget_s"),
        (budgets, "max_usd_per_job"),
    ):
        value = section.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise ValueError(f"{key} must not be negative, got {value!r}")

    domains = cfg.get("domains")
    if not isinstance(domains, dict):
        raise ValueError(f"domains must be an object, got {domains!r}")
    unknown = sorted(set(domains) - _KNOWN_DOMAINS)
    if unknown:
        raise ValueError(
            f"unknown domain(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(_KNOWN_DOMAINS))})"
        )

    # Warnings only from here on; none of these raise.
    if not cfg.get("api_token"):
        warnings.append("api_token is not set (set RSI_API_TOKEN or service/config.json)")
    if resolve_api_key(cfg) is None:
        env_name = fixer.get("api_key_env") or "DEEPSEEK_API_KEY"
        warnings.append(f"fixer API key env var {env_name} is not set")
    if (
        fixer.get("price_per_1m_input_usd") is None
        or fixer.get("price_per_1m_output_usd") is None
    ):
        warnings.append("fixer prices are not set; USD cost cannot be computed")
    if budgets.get("max_usd_per_job") is None:
        warnings.append("budgets.max_usd_per_job is not set; no per-job cost cap")
    host = cfg.get("host")
    if host not in ("127.0.0.1", "localhost", "::1"):
        warnings.append(f"host {host!r} is not loopback; the service will be reachable beyond this machine")
    if cfg.get("github", {}).get("create_pr") and resolve_github_token(cfg) is None:
        env_name = cfg.get("github", {}).get("token_env") or "GITHUB_TOKEN"
        warnings.append(f"github.create_pr is true but {env_name} is not set")
    return warnings


def resolve_api_key(cfg: dict) -> str | None:
    """Return the fixer API key from its configured env var, or None."""
    env_name = (cfg.get("fixer") or {}).get("api_key_env")
    if not env_name:
        return None
    return os.environ.get(env_name) or None


def resolve_github_token(cfg: dict) -> str | None:
    """Return the GitHub token from its configured env var, or None."""
    env_name = (cfg.get("github") or {}).get("token_env")
    if not env_name:
        return None
    return os.environ.get(env_name) or None


def resolve_data_dir(cfg: dict) -> Path:
    """Return the data dir, resolved relative to REPO_ROOT unless absolute."""
    data_dir = Path(cfg.get("data_dir") or DEFAULTS["data_dir"])
    if data_dir.is_absolute():
        return data_dir
    return REPO_ROOT / data_dir
