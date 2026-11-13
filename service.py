#!/usr/bin/env python3
"""Entry point for the morde-rsi HTTP service.

    python3 service.py serve [--config <path>]
    python3 service.py check-config [--config <path>]

`serve` loads the config, starts the Worker in a daemon thread and serves
the HTTP API in the main thread. `check-config` prints a summary without
any secret value (only "set"/"unset" for credentials) and exits non-zero
when the config is invalid or the API token is missing.

Note: this file is named service.py and the package lives in service/.
A same-named .py file wins over a namespace package on import, so the
service/ directory is registered as a namespace package below before any
`service.*` import (a no-op when service/__init__.py exists).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_service_package() -> None:
    """Make `import service.*` resolve to the service/ directory.

    See module docstring: without service/__init__.py, `import service`
    would load this very file instead of the package.
    """
    import importlib.machinery
    import importlib.util

    existing = sys.modules.get("service")
    if existing is not None and getattr(existing, "__path__", None):
        return
    spec = importlib.util.find_spec("service")
    if spec is not None and spec.submodule_search_locations is not None:
        return
    pkg_spec = importlib.machinery.ModuleSpec(
        "service", loader=None, is_package=True)
    pkg_spec.submodule_search_locations = [str(REPO_ROOT / "service")]
    sys.modules["service"] = importlib.util.module_from_spec(pkg_spec)


def _load_config(path):
    """Load + validate; returns (cfg, warnings) or raises ValueError."""
    from service.config import load_config, validate_config

    cfg = load_config(path)
    warnings = validate_config(cfg) or []
    return cfg, warnings


def _model_key_set(cfg: dict) -> bool:
    """Is a fixer model API key available (config value or env var)?"""
    fixer = cfg.get("fixer") or {}
    if fixer.get("api_key"):
        return True
    key_env = (fixer.get("api_key_env") or fixer.get("api_key_env_var")
               or "DEEPSEEK_API_KEY")
    return bool(os.environ.get(key_env))


def _cmd_serve(args) -> int:
    _ensure_service_package()
    try:
        cfg, warnings = _load_config(args.config)
    except ValueError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not cfg.get("api_token"):
        print("error: no api_token: set api_token in service/config.json "
              "or RSI_API_TOKEN env", file=sys.stderr)
        return 2

    from service.config import resolve_data_dir
    from service.server import run_server
    from service.store import JobStore
    from service.worker import Worker

    data_dir = resolve_data_dir(cfg)
    store = JobStore(data_dir / "jobs.sqlite3")
    worker = Worker(cfg, store)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 8765)
    print(f"morde-rsi listening on {host}:{port}")
    try:
        run_server(cfg, store)
    except KeyboardInterrupt:
        print("shutting down")
        return 0
    return 0


def _cmd_check_config(args) -> int:
    _ensure_service_package()
    try:
        cfg, warnings = _load_config(args.config)
    except ValueError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 1
    if not cfg.get("api_token"):
        print("error: api_token is missing: set api_token in "
              "service/config.json or RSI_API_TOKEN env", file=sys.stderr)
        return 1

    from service.config import resolve_data_dir

    fixer = cfg.get("fixer") or {}
    budgets = cfg.get("budgets") or {}
    github = cfg.get("github") or {}
    domains = cfg.get("domains") or {}

    print(f"host: {cfg.get('host', '127.0.0.1')}")
    print(f"port: {cfg.get('port', 8765)}")
    print(f"data_dir: {resolve_data_dir(cfg)}")
    print(f"fixer model: {fixer.get('model')}")
    print(f"fixer base_url: {fixer.get('base_url')}")
    print(f"budgets: {json.dumps(budgets, ensure_ascii=False)}")
    print(f"github.create_pr: {github.get('create_pr')}")
    print(f"domains: {', '.join(sorted(domains))}")
    print(f"api_token: {'set' if cfg.get('api_token') else 'unset'}")
    print(f"model api key: {'set' if _model_key_set(cfg) else 'unset'}")

    for warning in warnings:
        print(f"warning: {warning}")
    if not _model_key_set(cfg):
        print("warning: model API key is not set; the fixer cannot call "
              "its model until one is configured")
    token_env = github.get("token_env") or "GITHUB_TOKEN"
    if github.get("create_pr") and not os.environ.get(token_env) \
            and not github.get("token"):
        print(f"warning: github.create_pr is true but {token_env} is not set")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve", help="run the HTTP service + worker")
    p_serve.add_argument("--config", default=None)
    p_check = sub.add_parser(
        "check-config", help="validate config and print a summary")
    p_check.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _cmd_serve(args)
    return _cmd_check_config(args)


if __name__ == "__main__":
    sys.exit(main())
