from __future__ import annotations

import asyncio

import pytest


class TestRelayRpcDispatch:
    async def test_dict_params_passed_as_kwargs(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def add(self, a, b):
                return {"sum": a + b}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        assert await r.call("add", {"a": 2, "b": 3}) == {"sum": 5}

    async def test_list_params_passed_positionally(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def add(self, a, b):
                return {"sum": a + b}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        assert await r.call("add", [2, 3]) == {"sum": 5}

    async def test_getattr_dynamic_call(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def set_voltage(self, value):
                return {"voltage": value}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        assert await r.set_voltage(value=9.0) == {"voltage": 9.0}

    async def test_no_args_call(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def get_state(self):
                return {"ok": True}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        assert await r.get_state() == {"ok": True}

    async def test_unknown_method_raises_runtimeerror(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            pass

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        with pytest.raises(RuntimeError, match="unknown method"):
            await r.call("does_not_exist")

    async def test_exception_in_driver_method_propagates_as_rpc_error(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def boom(self):
                raise ValueError("kaboom")

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)
        r = await client.get_relay_agent("r1")
        with pytest.raises(RuntimeError, match="kaboom"):
            await r.call("boom")

    async def test_get_relay_agent_unknown_id_raises(self, mesh):
        broker = await mesh.start_broker()
        client = await mesh.start_client(broker)
        with pytest.raises(RuntimeError, match="not found"):
            await client.get_relay_agent("ghost")


class TestRelayStatePublishing:
    async def test_poll_is_published_periodically(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            def __init__(self):
                self.n = 0

            def poll(self):
                self.n += 1
                return {"n": self.n}

        await mesh.start_relay("r1", Driver(), broker, state_interval=0.05)
        client = await mesh.start_client(broker)

        events = []
        client.on_state(lambda rid, st: events.append((rid, st)))
        for _ in range(60):
            if events:
                break
            await asyncio.sleep(0.05)

        assert events, "expected at least one state update"
        rid, st = events[0]
        assert rid == "r1"
        assert "n" in st

    async def test_driver_without_poll_gets_default_state(self, mesh):
        broker = await mesh.start_broker()

        class Driver:
            pass  # deliberately no poll()

        await mesh.start_relay("r1", Driver(), broker, state_interval=0.05)
        client = await mesh.start_client(broker)

        events = []
        client.on_state(lambda rid, st: events.append((rid, st)))
        for _ in range(60):
            if events:
                break
            await asyncio.sleep(0.05)

        assert events
        rid, st = events[0]
        assert rid == "r1"
        assert st.get("relay_id") == "r1"
        assert "ts" in st
