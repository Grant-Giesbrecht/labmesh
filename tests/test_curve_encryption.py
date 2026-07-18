from __future__ import annotations

import asyncio

import pytest
import zmq

from labmesh import DirectorClientAgent
from labmesh.broker import _curve_server_setup as broker_server_setup
from labmesh.client import _curve_client_setup as client_setup


class TestCurveSetupHelpers:
    """Regression coverage for a real bug: os.environ.get(...) always
    returns str, but pyzmq's curve_secretkey/curve_publickey/curve_serverkey
    setters reject str outright ("unicode not allowed, use
    setsockopt_string") on modern pyzmq. Every _curve_*_setup helper in the
    codebase was assigning the raw str straight from the environment, so
    CURVE could never actually be enabled - it crashed with a TypeError
    the instant any node tried to bind or connect with it configured."""

    def test_server_setup_is_noop_without_env_vars(self, monkeypatch):
        monkeypatch.delenv("ZMQ_SERVER_SECRETKEY", raising=False)
        monkeypatch.delenv("ZMQ_SERVER_PUBLICKEY", raising=False)
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.ROUTER)
        try:
            broker_server_setup(s)
            assert s.get(zmq.CURVE_SERVER) == 0
        finally:
            s.close(0)

    def test_server_setup_enables_curve_from_str_env_vars(self, monkeypatch, curve_keys):
        # env vars are always plain str - this is the exact input shape
        # that used to crash every _curve_server_setup helper.
        monkeypatch.setenv("ZMQ_SERVER_SECRETKEY", curve_keys["server"]["secret"])
        monkeypatch.setenv("ZMQ_SERVER_PUBLICKEY", curve_keys["server"]["public"])
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.ROUTER)
        try:
            broker_server_setup(s)  # must not raise
            assert s.get(zmq.CURVE_SERVER) == 1
        finally:
            s.close(0)

    def test_client_setup_is_noop_with_partial_env_vars(self, monkeypatch, curve_keys):
        monkeypatch.setenv("ZMQ_CLIENT_SECRETKEY", curve_keys["client"]["secret"])
        monkeypatch.setenv("ZMQ_CLIENT_PUBLICKEY", curve_keys["client"]["public"])
        monkeypatch.delenv("ZMQ_SERVER_PUBLICKEY", raising=False)  # missing the third var
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.DEALER)
        try:
            client_setup(s)  # must not raise even though it does nothing
        finally:
            s.close(0)

    def test_client_setup_enables_curve_from_str_env_vars(self, monkeypatch, curve_keys):
        # CURVE_SECRETKEY/CURVE_SERVERKEY are write-only socket options in
        # libzmq (can't be read back), so the real assertion here is simply
        # that assigning str values - the exact shape os.environ.get()
        # returns - doesn't raise. See TestCurveEndToEnd for proof the
        # resulting socket actually completes a handshake.
        monkeypatch.setenv("ZMQ_CLIENT_SECRETKEY", curve_keys["client"]["secret"])
        monkeypatch.setenv("ZMQ_CLIENT_PUBLICKEY", curve_keys["client"]["public"])
        monkeypatch.setenv("ZMQ_SERVER_PUBLICKEY", curve_keys["server"]["public"])
        ctx = zmq.Context.instance()
        s = ctx.socket(zmq.DEALER)
        try:
            client_setup(s)  # must not raise
        finally:
            s.close(0)


class TestCurveEndToEnd:
    async def test_matching_keys_full_mesh_works(self, monkeypatch, mesh, curve_keys):
        monkeypatch.setenv("ZMQ_SERVER_SECRETKEY", curve_keys["server"]["secret"])
        monkeypatch.setenv("ZMQ_SERVER_PUBLICKEY", curve_keys["server"]["public"])
        monkeypatch.setenv("ZMQ_CLIENT_SECRETKEY", curve_keys["client"]["secret"])
        monkeypatch.setenv("ZMQ_CLIENT_PUBLICKEY", curve_keys["client"]["public"])

        broker = await mesh.start_broker()

        class Driver:
            def echo(self, value):
                return {"value": value}

        await mesh.start_relay("r1", Driver(), broker)
        client = await mesh.start_client(broker)

        assert [r["relay_id"] for r in await client.list_relay_ids()] == ["r1"]
        r = await client.get_relay_agent("r1")
        assert await r.echo(value=42) == {"value": 42}

    async def test_client_with_wrong_server_public_key_hangs_at_connect(self, monkeypatch, mesh, curve_keys):
        """Documented gotcha: a CURVE mismatch doesn't surface as a fast
        error - it just never completes the handshake. It's not only RPC
        calls that hang on this: DirectorClientAgent.connect() itself
        blocks on an un-timeboxed `recv()` waiting for the broker's hello
        ack (client.py's `_ = await req.recv()`), so a wrong server key
        hangs the *connection step*, before a caller gets anywhere near
        making a call. That's why a CURVE misconfiguration looks identical
        to "the broker isn't up yet" - bound the wait yourself."""
        monkeypatch.setenv("ZMQ_SERVER_SECRETKEY", curve_keys["server"]["secret"])
        monkeypatch.setenv("ZMQ_SERVER_PUBLICKEY", curve_keys["server"]["public"])
        monkeypatch.setenv("ZMQ_CLIENT_SECRETKEY", curve_keys["client"]["secret"])
        monkeypatch.setenv("ZMQ_CLIENT_PUBLICKEY", curve_keys["client"]["public"])

        broker = await mesh.start_broker()

        # the broker already bound its ROUTER with the *real* server keys
        # above; now swap the env so the client trusts a different one
        monkeypatch.setenv("ZMQ_SERVER_PUBLICKEY", curve_keys["client"]["public"])

        client = DirectorClientAgent(
            broker_address="127.0.0.1",
            broker_rpc=f"tcp://BROKER:{broker.rpc_port}",
            broker_xpub=f"tcp://BROKER:{broker.xpub_port}",
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.connect(), timeout=0.5)

        # connect() had already created and stored the sockets before it
        # got stuck on recv() - hand them to the mesh fixture for teardown
        mesh._sockets.extend([client.dir_req, client.sub])
