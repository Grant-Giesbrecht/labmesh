"""Minimal labmesh client - connects to the broker, looks up a relay by
ID, and calls one of its methods as a remote procedure call."""

import asyncio
from labmesh import DirectorClientAgent

async def main():
    client = DirectorClientAgent(
        broker_address="127.0.0.1",
        broker_rpc="tcp://BROKER:5750",
        broker_xpub="tcp://BROKER:5752",
    )
    await client.connect()

    counter = await client.get_relay_agent("counter-0")
    result = await counter.increment(by=5)
    print(result)  # {'n': 5}

if __name__ == "__main__":
    asyncio.run(main())
