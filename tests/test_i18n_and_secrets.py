"""Tests for i18n catalog loading + SecretStore encryption/decryption."""
from __future__ import annotations

import pytest


def test_i18n_fallback_without_catalog(monkeypatch, tmp_path):
    from bilibili_vision import i18n

    monkeypatch.setattr(i18n, "_LOCALE_DIR", tmp_path)
    monkeypatch.setattr(i18n, "_current", None)
    monkeypatch.setattr(i18n, "_current_lang", "")
    # No catalog exists — gettext() must return the source string.
    assert i18n.gettext("Extract") == "Extract"


def test_i18n_available_languages_ships_cn_and_en():
    from bilibili_vision import i18n

    langs = i18n.available_languages()
    assert "zh_CN" in langs
    assert "en_US" in langs


def test_i18n_switch_language():
    from bilibili_vision import i18n

    i18n.set_language("zh_CN")
    assert i18n.current_language() == "zh_CN"
    assert i18n.gettext("Extract") == "提取"
    i18n.set_language("en_US")
    assert i18n.gettext("Extract") == "Extract"
    i18n.set_language("")


def test_secret_store_plaintext_roundtrip(tmp_path):
    from bilibili_vision.secret_store import SecretStore

    p = tmp_path / "s.json"
    s = SecretStore(p)
    s.unlock(None)
    s.set("openai", "sk-test")
    s.set("gemini", "ya29-test")
    s.save()

    assert p.is_file()
    # Plaintext mode is readable as JSON.
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["openai"] == "sk-test"

    s2 = SecretStore(p)
    s2.unlock(None)
    assert s2.get("openai") == "sk-test"
    assert sorted(s2.keys()) == ["gemini", "openai"]


def test_secret_store_encrypted_roundtrip(tmp_path):
    from bilibili_vision.secret_store import SecretStore

    p = tmp_path / "s.enc"
    s = SecretStore(p)
    s.unlock("correct horse battery staple")
    s.set("openai", "sk-super-secret")
    s.save()

    raw = p.read_bytes()
    assert raw.startswith(b"VLV1")
    # The secret must not appear in plaintext anywhere in the file.
    assert b"sk-super-secret" not in raw

    s2 = SecretStore(p)
    s2.unlock("correct horse battery staple")
    assert s2.get("openai") == "sk-super-secret"


def test_secret_store_wrong_password_fails(tmp_path):
    from bilibili_vision.secret_store import SecretStore
    from bilibili_vision.errors import ConfigError

    p = tmp_path / "s.enc"
    s = SecretStore(p)
    s.unlock("right password")
    s.set("k", "v")
    s.save()

    s2 = SecretStore(p)
    with pytest.raises(ConfigError):
        s2.unlock("wrong password")


def test_secret_store_requires_unlock_before_read(tmp_path):
    from bilibili_vision.secret_store import SecretStore
    from bilibili_vision.errors import ConfigError

    s = SecretStore(tmp_path / "s")
    with pytest.raises(ConfigError):
        s.set("k", "v")
