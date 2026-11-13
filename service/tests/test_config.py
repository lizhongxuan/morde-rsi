import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from service import config as cfg_mod


def test_load_defaults_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("RSI_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = cfg_mod.load_config(tmp_path / "does-not-exist.json")
    assert cfg == cfg_mod.DEFAULTS
    # Loading must not mutate DEFAULTS.
    assert cfg_mod.DEFAULTS["api_token"] is None
    assert cfg_mod.DEFAULTS["port"] == 8765


def test_load_defaults_default_path_missing(monkeypatch):
    # No service/config.json exists in the repo yet: defaults apply.
    monkeypatch.delenv("RSI_API_TOKEN", raising=False)
    assert not (cfg_mod.REPO_ROOT / "service" / "config.json").exists()
    cfg = cfg_mod.load_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 8765
    assert cfg["api_token"] is None


def test_load_file_override_deep_merges(tmp_path, monkeypatch):
    monkeypatch.delenv("RSI_API_TOKEN", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "port": 9000,
        "fixer": {"model": "deepseek-reasoner", "timeout_s": 60},
        "budgets": {"max_usd_per_job": 1.5},
    }))
    cfg = cfg_mod.load_config(path)
    assert cfg["port"] == 9000
    assert cfg["host"] == "127.0.0.1"  # untouched default
    assert cfg["fixer"]["model"] == "deepseek-reasoner"
    assert cfg["fixer"]["timeout_s"] == 60
    assert cfg["fixer"]["base_url"] == "https://api.deepseek.com/v1"  # kept
    assert cfg["fixer"]["max_tokens"] == 8192  # kept
    assert cfg["budgets"]["max_usd_per_job"] == 1.5
    assert cfg["budgets"]["max_submissions"] == 3  # kept


def test_load_env_token_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_API_TOKEN", "tok-from-env")
    cfg = cfg_mod.load_config(tmp_path / "missing.json")
    assert cfg["api_token"] == "tok-from-env"


def test_load_env_token_empty_token_in_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_API_TOKEN", "tok-from-env")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_token": ""}))
    assert cfg_mod.load_config(path)["api_token"] == "tok-from-env"


def test_load_file_token_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_API_TOKEN", "tok-from-env")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_token": "tok-from-file"}))
    assert cfg_mod.load_config(path)["api_token"] == "tok-from-file"


def test_resolve_api_key_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert cfg_mod.resolve_api_key(cfg_mod.DEFAULTS) == "sk-test"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert cfg_mod.resolve_api_key(cfg_mod.DEFAULTS) is None
    # Custom env var name is honoured.
    cfg = copy.deepcopy(cfg_mod.DEFAULTS)
    cfg["fixer"]["api_key_env"] = "MY_KEY_ENV"
    monkeypatch.setenv("MY_KEY_ENV", "sk-custom")
    assert cfg_mod.resolve_api_key(cfg) == "sk-custom"


def test_resolve_github_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
    assert cfg_mod.resolve_github_token(cfg_mod.DEFAULTS) == "ghp-test"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cfg_mod.resolve_github_token(cfg_mod.DEFAULTS) is None


def test_resolve_data_dir():
    cfg = copy.deepcopy(cfg_mod.DEFAULTS)
    assert cfg_mod.resolve_data_dir(cfg) == cfg_mod.REPO_ROOT / "runs" / "service"
    cfg["data_dir"] = "/tmp/abs-data"
    assert cfg_mod.resolve_data_dir(cfg) == Path("/tmp/abs-data")
    cfg["data_dir"] = "custom/dir"
    assert cfg_mod.resolve_data_dir(cfg) == cfg_mod.REPO_ROOT / "custom" / "dir"


def test_validate_valid_returns_warnings_list(monkeypatch):
    # Defaults are valid: no hard error. Warnings are allowed (no token,
    # no prices, no cost cap in the defaults).
    monkeypatch.delenv("RSI_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    warnings = cfg_mod.validate_config(copy.deepcopy(cfg_mod.DEFAULTS))
    assert isinstance(warnings, list)
    assert any("api_token" in w for w in warnings)


@pytest.mark.parametrize("bad_port", [0, 65536, -1, "8765", 8765.5, True, None])
def test_validate_port_errors(bad_port):
    cfg = copy.deepcopy(cfg_mod.DEFAULTS)
    cfg["port"] = bad_port
    with pytest.raises(ValueError, match="port"):
        cfg_mod.validate_config(cfg)


@pytest.mark.parametrize("section,key,value", [
    ("budgets", "max_submissions", -1),
    ("budgets", "time_budget_s", -5),
    ("budgets", "max_usd_per_job", -0.5),
    ("fixer", "timeout_s", -1),
    ("fixer", "max_tokens", -100),
])
def test_validate_negative_errors(section, key, value):
    cfg = copy.deepcopy(cfg_mod.DEFAULTS)
    cfg[section][key] = value
    with pytest.raises(ValueError, match=key):
        cfg_mod.validate_config(cfg)


@pytest.mark.parametrize("bad_domains", [None, "morde", ["morde"], {"other": {}}, {"morde": {}, "evil": {}}])
def test_validate_domains_errors(bad_domains):
    cfg = copy.deepcopy(cfg_mod.DEFAULTS)
    cfg["domains"] = bad_domains
    with pytest.raises(ValueError, match="domain"):
        cfg_mod.validate_config(cfg)


def test_validate_domains_subset_ok():
    cfg = copy.deepcopy(cfg_mod.DEFAULTS)
    cfg["domains"] = {"self": {"repo": "/tmp/x"}}
    assert isinstance(cfg_mod.validate_config(cfg), list)


def test_example_config_matches_defaults_shape():
    example = json.loads(
        (cfg_mod.REPO_ROOT / "service" / "config.example.json").read_text()
    )
    assert example["api_token"] == "CHANGE-ME-OR-SET-RSI_API_TOKEN"
    assert example["github"]["create_pr"] is False
    assert example["fixer"]["price_per_1m_input_usd"] is None
    assert example["fixer"]["price_per_1m_output_usd"] is None
    assert "_comment" in example
    keys = {k for k in example if not k.startswith("_")}
    assert keys == set(cfg_mod.DEFAULTS)
    # Loading the example must validate (after dropping _comment).
    cfg = cfg_mod.load_config(cfg_mod.REPO_ROOT / "service" / "config.example.json")
    assert "_comment" not in cfg
    assert isinstance(cfg_mod.validate_config(cfg), list)
