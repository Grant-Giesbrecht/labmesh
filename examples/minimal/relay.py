"""Minimal labmesh relay - wraps a plain Python object and exposes its
public methods as remote procedure calls. No instrument, no databank."""

import asyncio
from typing import Any, Dict
from labmesh import RelayAgent

class Counter:
    """Any plain object works as a driver - labmesh exposes its public
    methods over RPC by name, and calls poll() (if present) on a timer
    to publish state."""

    def __init__(self):
        self.n = 0

    def increment(self, by: int = 1) -> Dict[str, Any]:
        self.n += by
        return {"n": self.n}

    def poll(self) -> Dict[str, Any]:
        return {"n": self.n}

async def main():
    agent = RelayAgent(
        "counter-0", Counter(),
        broker_rpc="tcp://BROKER:5750",
        rpc_bind="tcp://*:5850",
        state_pub="tcp://BROKER:5751",
        broker_address="127.0.0.1",
    )
    await agent.run()

if __name__ == "__main__":
    asyncio.run(main())
