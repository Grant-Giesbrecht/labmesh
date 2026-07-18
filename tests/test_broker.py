from __future__ import annotations

import asyncio
import uuid

import pytest

from labmesh.util import dumps, loads


async def _rpc(sock, method, params=None, timeout=2.0):
    rpc_uuid = uuid.uuid4().hex
    await sock.send(dumps({"type": "rpc", "rpc_uuid": rpc_uuid, "method": method, "params": params or {}}))
    while True:
        msg = loads(await asyncio.wait_for(sock.recv(), timeout=timeout))
        if msg.get("rpc_uuid") == rpc_uuid:
            return msg


class TestRelayAndBankRegistration:
    async def test_relay_appears_in_directory(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            pass

        await mesh.start_relay("Inst-0", Driver(), broker)
        client = await mesh.start_client(broker)

        relays = await client.list_relay_ids()
        assert [r["relay_id"] for r in relays] == ["Inst-0"]
        assert relays[0]["rpc_endpoint"].startswith("tcp://127.0.0.1:")

    async def test_bank_appears_in_directory(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        await mesh.start_databank("bank-0", tmp_path, broker)
        client = await mesh.start_client(broker)

        banks = await client.list_banks()
        assert [b["bank_id"] for b in banks] == ["bank-0"]
        assert "ingest" in banks[0] and "retrieve" in banks[0]

    async def test_multiple_relays_sorted_by_id(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            pass

        await mesh.start_relay("Inst-b", Driver(), broker)
        await mesh.start_relay("Inst-a", Driver(), broker)
        client = await mesh.start_client(broker)

        relays = await client.list_relay_ids()
        assert [r["relay_id"] for r in relays] == ["Inst-a", "Inst-b"]

    async def test_empty_directory_before_anyone_registers(self, mesh):
        broker = await mesh.start_broker()
        client = await mesh.start_client(broker)

        assert await client.list_relay_ids() == []
        assert await client.list_banks() == []


class TestDirectoryRpc:
    async def test_ping(self, mesh, dealer_to):
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        reply = await _rpc(sock, "ping")
        assert reply == {"type": "rpc_result", "rpc_uuid": reply["rpc_uuid"], "result": {"ok": True}}

    async def test_unknown_method_is_rpc_error(self, mesh, dealer_to):
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        reply = await _rpc(sock, "not_a_real_method")
        assert reply["type"] == "rpc_error"
        assert reply["error"]["code"] == 400

    async def test_relay_hello_missing_relay_id_is_rejected(self, mesh, dealer_to):
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        await sock.send(dumps({"type": "hello", "role": "relay", "rpc_endpoint": "tcp://127.0.0.1:5850"}))
        reply = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))
        assert reply["type"] == "error"
        assert reply["error"]["code"] == 400
        assert "relay_id" in reply["error"]["message"]

    async def test_unrecognized_role_falls_back_to_client(self, mesh, dealer_to):
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        await sock.send(dumps({"type": "hello", "role": "some-typo"}))
        reply = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))
        assert reply == {"type": "hello", "ok": True, "role": "client"}

    async def test_hello_with_no_role_falls_back_to_client(self, mesh, dealer_to):
        broker = await mesh.start_broker()
        sock = dealer_to(f"tcp://127.0.0.1:{broker.rpc_port}")
        await sock.send(dumps({"type": "hello"}))
        reply = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))
        assert reply == {"type": "hello", "ok": True, "role": "client"}
