from __future__ import annotations

import json
import re

import pytest

from labmesh.security import generate_curve_keypair, main as keygen_main


class TestGenerateCurveKeypair:
    def test_returns_public_and_secret(self):
        pair = generate_curve_keypair()
        assert set(pair.keys()) == {"public", "secret"}

    def test_keys_are_well_formed_z85(self):
        # zmq.curve_keypair() returns 40-byte Z85-encoded strings
        pair = generate_curve_keypair()
        assert isinstance(pair["public"], str) and len(pair["public"]) == 40
        assert isinstance(pair["secret"], str) and len(pair["secret"]) == 40

    def test_successive_calls_are_different(self):
        a, b = generate_curve_keypair(), generate_curve_keypair()
        assert a["public"] != b["public"]
        assert a["secret"] != b["secret"]


class TestKeygenCli:
    """Regression coverage for the `p`/`parser` NameError that made every
    invocation of `lm-keygen` crash regardless of arguments."""

    def test_default_format_is_env(self, capsys):
        keygen_main([])
        out = capsys.readouterr().out
        assert re.search(r'^ZMQ_PUBLICKEY="[^"]{40}"$', out, re.MULTILINE)
        assert re.search(r'^ZMQ_SECRETKEY="[^"]{40}"$', out, re.MULTILINE)

    def test_format_env_explicit(self, capsys):
        keygen_main(["--format", "env"])
        out = capsys.readouterr().out
        assert "ZMQ_PUBLICKEY=" in out
        assert "ZMQ_SECRETKEY=" in out

    def test_format_json(self, capsys):
        keygen_main(["--format", "json"])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert set(parsed.keys()) == {"public", "secret"}
        assert len(parsed["public"]) == 40
        assert len(parsed["secret"]) == 40

    def test_format_toml_default_section(self, capsys):
        keygen_main(["--format", "toml"])
        out = capsys.readouterr().out
        assert out.startswith("[curve.server]\n")
        assert re.search(r'^public = "[^"]{40}"$', out, re.MULTILINE)
        assert re.search(r'^secret = "[^"]{40}"$', out, re.MULTILINE)

    def test_format_toml_custom_section(self, capsys):
        keygen_main(["--format", "toml", "--section", "broker.curve.server"])
        out = capsys.readouterr().out
        assert out.startswith("[broker.curve.server]\n")

    def test_invalid_format_rejected(self):
        with pytest.raises(SystemExit):
            keygen_main(["--format", "bogus"])
