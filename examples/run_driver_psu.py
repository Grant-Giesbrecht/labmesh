
import asyncio, sys, time, random, os
from typing import Dict, Any
from labmesh import RelayAgent
from labmesh.relay import upload_dataset

class MockPSU:
    def __init__(self, global_name: str):
        self.gname = global_name
        self.voltage = 0.0
        self.current = 0.0
        self.output = False
        self.last_updated = time.time()
        self._count = 0

    def set_voltage(self, value: float) -> Dict[str, Any]:
        self.voltage = float(value); self.last_updated = time.time()
        return {"ok": True, "voltage": self.voltage}

    def set_output(self, on: bool) -> Dict[str, Any]:
        self.output = bool(on); self.last_updated = time.time()
        return {"ok": True, "output": self.output}

    def get_state(self) -> Dict[str, Any]:
        return {"global_name": self.gname, "voltage": self.voltage, "current": self.current,
                "output": self.output, "last_updated": self.last_updated, "runs": self._count}

    def poll(self) -> Dict[str, Any]:
        self.current = round(self.voltage * (0.9 + 0.2 * random.random()), 4)
        return self.get_state()

async def periodic_upload(global_name: str):
    # pretend a big result every ~5s
    ingest = os.environ.get("LMH_BANK_INGEST_CONNECT", "tcp://127.0.0.1:5761")
    n = 0
    while True:
        await asyncio.sleep(60)
        payload = ("Result %d from %s\n" % (n, global_name)).encode() * 200000  # ~4MB
        did = await upload_dataset(ingest, payload, global_name=global_name, meta={"note":"demo"})
        print(f"[relay:{global_name}] uploaded dataset {did}")
        n += 1

async def main():
    gname = sys.argv[1] if len(sys.argv) > 1 else "psu-1"
    agent = RelayAgent(gname, MockPSU(gname), state_interval=1.0)
    await asyncio.gather(agent.run(), periodic_upload(gname))

if __name__ == "__main__":
    asyncio.run(main())
