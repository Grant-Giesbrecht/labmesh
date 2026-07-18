"""Minimal labmesh broker - no config file, no security, three bound ports."""

import asyncio
from labmesh import DirectoryBroker

async def main():
    broker = DirectoryBroker("tcp://*:5750", "tcp://*:5751", "tcp://*:5752")
    await broker.serve()

if __name__ == "__main__":
    asyncio.run(main())
