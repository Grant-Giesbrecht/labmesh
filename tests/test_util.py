from __future__ import annotations

import os
import pytest

from labmesh.util import (
    dumps, loads, env_int, read_toml_config,
    network_password, check_password, prompt_network_password,
)


class TestDumpsLoads:
    def test_roundtrip(self):
        obj = {"type": "rpc", "rpc_uuid": "abc123", "method": "set_voltage", "params": {"value": 2.5}}
        assert loads(dumps(obj)) == obj

    def test_dumps_returns_compact_bytes(self):
        # no spaces after separators - the wire format is meant to be compact
        raw = dumps({"a": 1, "b": [1, 2]})
        assert isinstance(raw, bytes)
        assert b" " not in raw

    def test_loads_rejects_garbage(self):
        with pytest.raises(Exception):
            loads(b"not json")


class TestEnvInt:
    def test_uses_env_value(self, monkeypatch):
        monkeypatch.setenv("LMH_TEST_INT", "42")
        assert env_int("LMH_TEST_INT", 7) == 42

    def test_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("LMH_TEST_INT", raising=False)
        assert env_int("LMH_TEST_INT", 7) == 7

    def test_falls_back_to_default_when_unparseable(self, monkeypatch):
        monkeypatch.setenv("LMH_TEST_INT", "not-a-number")
        assert env_int("LMH_TEST_INT", 7) == 7


class TestReadTomlConfig:
    def test_reads_nested_tables(self, tmp_path):
        toml_path = tmp_path / "labmesh.toml"
        toml_path.write_text(
            '[broker]\n'
            'rpc_bind = "tcp://*:5750"\n'
            'ttl_seconds = 30\n',
            encoding="utf-8",
        )
        cfg = read_toml_config(str(toml_path))
        assert cfg["broker"]["rpc_bind"] == "tcp://*:5750"
        assert cfg["broker"]["ttl_seconds"] == 30

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            read_toml_config(str(tmp_path / "does_not_exist.toml"))


class TestNetworkPassword:
    def test_empty_when_unset(self, monkeypatch):
        monkeypatch.delenv("LMH_NETWORK_PASSWORD", raising=False)
        assert network_password() == ""

    def test_reads_env_value(self, monkeypatch):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "hunter2")
        assert network_password() == "hunter2"


class TestCheckPassword:
    def test_matching_password_passes(self):
        assert check_password({"password": "hunter2"}, "hunter2") is True

    def test_wrong_password_fails(self):
        assert check_password({"password": "wrong"}, "hunter2") is False

    def test_missing_password_field_fails(self):
        assert check_password({}, "hunter2") is False

    def test_none_password_field_fails(self):
        assert check_password({"password": None}, "hunter2") is False

    def test_both_empty_passes(self):
        # an unconfigured node (expected == "") checking a message with no
        # password field should never be reached in practice (callers skip
        # the check entirely when `expected` is falsy) but the comparison
        # itself is still well-defined and symmetric.
        assert check_password({"password": ""}, "") is True


class TestPromptNetworkPassword:
    def test_returns_existing_value_without_prompting(self, monkeypatch):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "already-set")
        called = {"n": 0}
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "should-not-be-used")
        assert prompt_network_password() == "already-set"
        assert called["n"] == 0

    def test_explicit_empty_string_skips_prompt(self, monkeypatch):
        # LMH_NETWORK_PASSWORD="" (explicitly set, e.g. by a script) must be
        # treated as "auth disabled, non-interactively" - not as "unset".
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "")
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: pytest.fail("should not prompt"))
        assert prompt_network_password() == ""

    def test_prompts_when_unset_and_stores_result(self, monkeypatch):
        monkeypatch.delenv("LMH_NETWORK_PASSWORD", raising=False)
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: "typed-password")
        result = prompt_network_password()
        assert result == "typed-password"
        assert os.environ["LMH_NETWORK_PASSWORD"] == "typed-password"

    def test_confirm_mismatch_raises(self, monkeypatch):
        monkeypatch.delenv("LMH_NETWORK_PASSWORD", raising=False)
        answers = iter(["first-try", "different-second-try"])
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
        with pytest.raises(SystemExit):
            prompt_network_password(confirm=True)

    def test_confirm_match_succeeds(self, monkeypatch):
        monkeypatch.delenv("LMH_NETWORK_PASSWORD", raising=False)
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: "same-password")
        assert prompt_network_password(confirm=True) == "same-password"
