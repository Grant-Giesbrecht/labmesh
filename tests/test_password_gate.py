from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

from labmesh.relay import upload_dataset
from labmesh.util import dumps, loads


async def _send_recv(sock, obj, timeout=2.0):
    await sock.send(dumps(obj))
    return loads(await asyncio.wait_for(sock.recv(), timeout=timeout))


class TestPasswordEnforcedEndToEnd:
    """The common deployment: every node's shell has the identical
    LMH_NETWORK_PASSWORD exported, matching what's documented in
    architecture.rst."""

    async def test_full_mesh_works_with_matching_password(self, monkeypatch, mesh, tmp_path):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "correct-horse-battery-staple")
        broker = await mesh.start_broker()

        class Driver:
            def set_voltage(self, value):
                return {"voltage": value}

        await mesh.start_relay("r1", Driver(), broker)
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        client = await mesh.start_client(broker)

        assert [r["relay_id"] for r in await client.list_relay_ids()] == ["r1"]

        r = await client.get_relay_agent("r1")
        assert await r.set_voltage(value=3.3) == {"voltage": 3.3}

        payload = b"secured payload"
        dataset_id = await upload_dataset(f"tcp://127.0.0.1:{bank.ingest_port}", payload, relay_id="r1")
        bc = await client.get_databank_agent("bank-0")
        dest = tmp_path / "out.bin"
        await bc.download(dataset_id, str(dest))
        assert dest.read_bytes() == payload

    async def test_disabled_everywhere_is_unchanged_legacy_behavior(self, monkeypatch, mesh):
        # explicitly empty, not just unset - exercises the same "no prompt,
        # no check" path a scripted/CI boot relies on
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "")
        broker = await mesh.start_broker()

        class Driver:
            def ping(self):
                return {"ok": True}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        assert await r.ping() == {"ok": True}


class TestWrongOrMissingPasswordRejected:
    """Crafted directly with a raw DEALER socket so the password field can
    be wrong or absent independently of what the node under test expects."""

    async def test_broker_hello_wrong_password(self, monkeypatch, mesh, dealer_to):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        reply = await _send_recv(sock, {"type": "hello", "role": "client", "password": "guess"})
        assert reply == {"type": "hello", "ok": False, "error": "invalid password"}

    async def test_broker_rpc_wrong_password(self, monkeypatch, mesh, dealer_to):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        rpc_uuid = uuid.uuid4().hex
        reply = await _send_recv(sock, {"type": "rpc", "rpc_uuid": rpc_uuid, "method": "ping", "password": "guess"})
        assert reply["type"] == "rpc_error"
        assert reply["rpc_uuid"] == rpc_uuid
        assert reply["error"] == {"code": 401, "message": "invalid password"}

    async def test_broker_rpc_missing_password_field(self, monkeypatch, mesh, dealer_to):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        reply = await _send_recv(sock, {"type": "rpc", "rpc_uuid": "x", "method": "ping"})
        assert reply["type"] == "rpc_error"
        assert reply["error"]["code"] == 401

    async def test_relay_rpc_wrong_password(self, monkeypatch, mesh, dealer_to):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()

        class Driver:
            def secret_method(self):
                return {"leaked": True}

        relay = await mesh.start_relay("r1", Driver(), broker)
        sock = dealer_to(f"tcp://127.0.0.1:{relay.rpc_port}")
        rpc_uuid = uuid.uuid4().hex
        reply = await _send_recv(sock, {"type": "rpc", "rpc_uuid": rpc_uuid, "method": "secret_method", "password": "guess"})
        assert reply == {"type": "rpc_error", "rpc_uuid": rpc_uuid, "error": {"code": 401, "message": "invalid password"}}

    async def test_databank_ingest_start_wrong_password_rejected_before_file_opened(self, monkeypatch, mesh, dealer_to, tmp_path):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        sock = dealer_to(f"tcp://127.0.0.1:{bank.ingest_port}")

        reply = await _send_recv(sock, {"type": "ingest_start", "dataset_id": "d1", "relay_id": "r1", "meta": {}, "password": "guess"})
        assert reply["type"] == "error"
        assert reply["error"]["code"] == 401
        assert not (tmp_path / "d1.bin").exists()

    async def test_databank_get_wrong_password(self, monkeypatch, mesh, dealer_to, tmp_path):
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        sock = dealer_to(f"tcp://127.0.0.1:{bank.retrieve_port}")

        reply = await _send_recv(sock, {"type": "get", "dataset_id": "whatever", "password": "guess"})
        assert reply["type"] == "error"
        assert reply["error"]["code"] == 401

    async def test_high_level_client_raises_runtimeerror_on_wrong_password(self, monkeypatch, mesh):
        # the client's own network_password() legitimately returns the
        # wrong value here - simulating a client shell that was given a
        # stale or mistyped password
        monkeypatch.setenv("LMH_NETWORK_PASSWORD", "the-real-password")
        broker = await mesh.start_broker()
        client = await mesh.start_client(broker)

        monkeypatch.setattr("labmesh.client.network_password", lambda: "wrong-guess")
        with pytest.raises(RuntimeError):
            await client.list_relay_ids()


class TestPerNodeOptIn:
    """Each gate reads its own process's LMH_NETWORK_PASSWORD independently
    - there is no central switch. In-process, every node shares one
    os.environ, so this simulates "different processes" by patching the
    network_password name bound inside each module rather than the env var."""

    async def test_relay_with_no_password_configured_accepts_any_caller_even_if_broker_requires_one(self, monkeypatch, mesh, dealer_to):
        monkeypatch.setattr("labmesh.broker.network_password", lambda: "broker-only-secret")
        # labmesh.relay.network_password is left alone -> reads the real
        # (unset) env var -> that relay's own gate is disabled

        broker = await mesh.start_broker()

        class Driver:
            def open_method(self):
                return {"reached": True}

        relay = await mesh.start_relay("r1", Driver(), broker)

        # broker's directory does require the password
        broker_sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        reply = await _send_recv(broker_sock, {"type": "rpc", "rpc_uuid": "x", "method": "ping"})
        assert reply["type"] == "rpc_error"

        # but the relay itself never got a password configured, so a
        # direct call with no password field at all still goes through
        relay_sock = dealer_to(f"tcp://127.0.0.1:{relay.rpc_port}")
        rpc_uuid = uuid.uuid4().hex
        reply = await _send_recv(relay_sock, {"type": "rpc", "rpc_uuid": rpc_uuid, "method": "open_method"})
        assert reply == {"type": "rpc_result", "rpc_uuid": rpc_uuid, "result": {"reached": True}}
