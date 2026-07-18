from __future__ import annotations

import asyncio
import contextlib
import os
import socket

import pytest
import pytest_asyncio
import zmq
import zmq.asyncio

from labmesh import DirectoryBroker, RelayAgent, DataBank, DirectorClientAgent
from labmesh.security import generate_curve_keypair

# Every env var any node's CURVE/password helpers read. Scrubbed before and
# after each test so one test's security config can never leak into another.
_SECURITY_ENV_VARS = [
    "ZMQ_SERVER_SECRETKEY", "ZMQ_SERVER_PUBLICKEY",
    "ZMQ_CLIENT_SECRETKEY", "ZMQ_CLIENT_PUBLICKEY",
    "LMH_NETWORK_PASSWORD",
]


@pytest.fixture(autouse=True)
def clean_security_env():
    saved = {k: os.environ.get(k) for k in _SECURITY_ENV_VARS}
    for k in _SECURITY_ENV_VARS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _free_port() -> int:
    """Ask the OS for an unused loopback TCP port by binding to port 0."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def free_port():
    return _free_port


async def _settle():
    """Give a just-scheduled node's bind()/connect() calls a moment to run."""
    await asyncio.sleep(0.15)


class Mesh:
    """Starts broker/relay/databank/client nodes as background asyncio tasks
    on the shared zmq.asyncio.Context, and tears every one of them down
    (cancel the task, close whatever sockets it created) at the end of a
    test. Nothing in labmesh itself closes sockets on the happy path, so
    the test harness has to.
    """

    def __init__(self, broker_address: str = "127.0.0.1"):
        self.broker_address = broker_address
        self._tasks: list[asyncio.Task] = []
        self._sockets: list = []

    def _track(self, task, obj, sock_attrs):
        self._tasks.append(task)
        for a in sock_attrs:
            self._sockets.append(getattr(obj, a, None))

    async def start_broker(self) -> DirectoryBroker:
        rpc, xsub, xpub = _free_port(), _free_port(), _free_port()
        broker = DirectoryBroker(f"tcp://*:{rpc}", f"tcp://*:{xsub}", f"tcp://*:{xpub}")
        task = asyncio.create_task(broker.serve())
        await _settle()
        self._track(task, broker, ["_router", "_xsub", "_xpub"])
        broker.rpc_port, broker.xsub_port, broker.xpub_port = rpc, xsub, xpub
        return broker

    async def start_relay(self, relay_id, driver, broker: DirectoryBroker, *, state_interval: float = 0.05) -> RelayAgent:
        rpc = _free_port()
        agent = RelayAgent(
            relay_id, driver,
            broker_rpc=f"tcp://BROKER:{broker.rpc_port}",
            rpc_bind=f"tcp://*:{rpc}",
            state_pub=f"tcp://BROKER:{broker.xsub_port}",
            broker_address=self.broker_address,
            state_interval=state_interval,
        )
        task = asyncio.create_task(agent.run())
        await _settle()
        self._track(task, agent, ["router", "pub", "dir_req"])
        agent.rpc_port = rpc
        return agent

    async def start_databank(self, bank_id, data_dir, broker: DirectoryBroker, *, heartbeat_sec: float = 5) -> DataBank:
        ingest, retrieve = _free_port(), _free_port()
        bank = DataBank(
            ingest_bind=f"tcp://*:{ingest}",
            retrieve_bind=f"tcp://*:{retrieve}",
            data_dir=str(data_dir),
            broker_rpc=f"tcp://BROKER:{broker.rpc_port}",
            broker_xsub=f"tcp://BROKER:{broker.xsub_port}",
            bank_id=bank_id,
            heartbeat_sec=heartbeat_sec,
            broker_address=self.broker_address,
        )
        task = asyncio.create_task(bank.serve())
        await _settle()
        self._track(task, bank, ["ingest_router", "retrieve_router", "pub", "rpc_sock"])
        bank.ingest_port, bank.retrieve_port = ingest, retrieve
        return bank

    async def start_client(self, broker: DirectoryBroker) -> DirectorClientAgent:
        client = DirectorClientAgent(
            broker_address=self.broker_address,
            broker_rpc=f"tcp://BROKER:{broker.rpc_port}",
            broker_xpub=f"tcp://BROKER:{broker.xpub_port}",
        )
        await client.connect()
        await _settle()
        self._sockets.extend([client.dir_req, client.sub])
        return client

    async def teardown(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for s in self._sockets:
            if s is not None:
                with contextlib.suppress(Exception):
                    s.close(0)


@pytest_asyncio.fixture
async def mesh():
    m = Mesh()
    try:
        yield m
    finally:
        await m.teardown()


@pytest.fixture
def curve_keys():
    """Two independent keypairs, generated the same way lm-keygen does."""
    return {"server": generate_curve_keypair(), "client": generate_curve_keypair()}


@pytest_asyncio.fixture
async def dealer_to(mesh):
    """Factory for a raw DEALER socket connected to an address, for tests
    that need to send hand-crafted envelopes the high-level API doesn't
    expose (malformed hello messages, wrong passwords, etc). Torn down by
    the same `mesh` fixture as everything else in the test.
    """
    ctx = zmq.asyncio.Context.instance()

    def _make(addr: str):
        s = ctx.socket(zmq.DEALER)
        s.connect(addr)
        mesh._sockets.append(s)
        return s

    return _make
