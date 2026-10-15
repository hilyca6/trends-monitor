import json

from collector import settings
from collector.push import dingtalk_sign


def test_load_config_merges_defaults(tmp_path):
    p = tmp_path / "config.local.json"
    p.write_text(json.dumps({"proxy": "http://127.0.0.1:7897", "llm": {"api_key": "abc"}}), encoding="utf-8")
    cfg = settings.load_config(p)
    assert cfg["proxy"] == "http://127.0.0.1:7897"
    assert cfg["llm"]["api_key"] == "abc"
    assert cfg["llm"]["model"] == "glm-4-flash"      # default kept
    assert cfg["games"]["enabled"] is True           # default kept


def test_apply_environment_sets_openai_not_deepseek(tmp_path, monkeypatch):
    cfg = {
        "proxy": "http://127.0.0.1:7897",
        "llm": {"api_key": "key123", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    }
    monkeypatch.setenv("DEEPSEEK_API_KEY", "should-be-removed")
    settings.apply_environment(cfg)
    import os

    assert os.environ["OPENAI_API_KEY"] == "key123"
    assert os.environ["OPENAI_MODEL"] == "glm-4-flash"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_apply_environment_without_key_leaves_llm_unset(monkeypatch):
    import os

    for v in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    settings.apply_environment({"proxy": "", "llm": {"api_key": ""}})
    assert "OPENAI_API_KEY" not in os.environ


def test_apply_environment_disabled_llm_leaves_key_unset(monkeypatch):
    import os

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings.apply_environment({"proxy": "", "llm": {"enabled": False, "api_key": "key123"}})
    assert "OPENAI_API_KEY" not in os.environ


def test_dingtalk_sign_is_deterministic():
    a = dingtalk_sign("SECxxx", "1700000000000")
    b = dingtalk_sign("SECxxx", "1700000000000")
    assert a == b
    assert a != dingtalk_sign("SECxxx", "1700000000001")
    # URL-encoded base64 (no raw '+', '/', '=')
    assert "+" not in a and "/" not in a
