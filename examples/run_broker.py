
import asyncio
from labmesh import DirectoryBroker

if __name__ == "__main__":
    asyncio.run(DirectoryBroker().serve())
