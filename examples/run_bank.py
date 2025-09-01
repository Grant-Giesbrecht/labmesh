
import asyncio
from labmesh import DataBank

if __name__ == "__main__":
    asyncio.run(DataBank().serve())
