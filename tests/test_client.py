from __future__ import annotations

import asyncio

import pytest


class TestDirectorClientAgent:
    async def test_get_databank_agent_picks_first_when_no_id_given(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        await mesh.start_databank("bank-a", tmp_path, broker)
        client = await mesh.start_client(broker)
        bc = await client.get_databank_agent()
        assert bc.retrieve_endpoint

    async def test_get_databank_agent_raises_when_no_banks_registered(self, mesh):
        broker = await mesh.start_broker()
        client = await mesh.start_client(broker)
        with pytest.raises(RuntimeError, match="no banks"):
            await client.get_databank_agent()

    async def test_get_databank_agent_unknown_id_raises(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        await mesh.start_databank("bank-a", tmp_path, broker)
        client = await mesh.start_client(broker)
        with pytest.raises(RuntimeError, match="not found"):
            await client.get_databank_agent("ghost-bank")

    async def test_on_state_supports_multiple_callbacks(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def poll(self):
                return {"v": 1}

        await mesh.start_relay("r1", Driver(), broker, state_interval=0.05)
        client = await mesh.start_client(broker)

        seen_a, seen_b = [], []
        client.on_state(lambda rid, st: seen_a.append(rid))
        client.on_state(lambda rid, st: seen_b.append(rid))

        for _ in range(60):
            if seen_a and seen_b:
                break
            await asyncio.sleep(0.05)

        assert seen_a and seen_b

    async def test_relay_client_demultiplexes_concurrent_calls_by_rpc_uuid(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def echo(self, value):
                return {"value": value}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")

        results = await asyncio.gather(*(r.echo(value=i) for i in range(20)))
        assert [res["value"] for res in results] == list(range(20))

    async def test_list_relay_ids_reflects_current_registrations_only(self, mesh):
        broker = await mesh.start_broker()
        client = await mesh.start_client(broker)
        assert await client.list_relay_ids() == []

        class Driver:
            pass

        await mesh.start_relay("late-arriving", Driver(), broker)
        # give the relay's hello a moment to land at the broker
        for _ in range(40):
            if await client.list_relay_ids():
                break
            await asyncio.sleep(0.05)

        assert [r["relay_id"] for r in await client.list_relay_ids()] == ["late-arriving"]
