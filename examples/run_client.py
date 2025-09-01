
import asyncio, os
from labmesh import LabClient, DriverClient

class PSUClient(DriverClient):
    async def set_voltage(self, *, value: float):
        return await super().call("set_voltage", {"value": value})
    async def set_output(self, *, on: bool):
        return await super().call("set_output", {"on": on})
    async def get_state(self):
        return await super().call("get_state", {})

async def main():
    client = LabClient()
    await client.connect()

    print("Services:", await client.list_services())
    print("Banks:", await client.list_banks())

    client.on_state(lambda svc, st: print(f"[state] {svc}: {st}"))
    client.on_dataset(lambda info: print(f"[dataset] new {info}"))

    psu = await client.driver("psu-1")
    await psu.set_voltage(value=2.5)

    # auto-pick first bank and download datasets as they appear
    async def downloader(info):
        bank = await client.bank(info.get("bank_id"))
        dest = f"./download_{info['dataset_id']}.bin"
        meta = await bank.download(info["dataset_id"], dest)
        print("[downloaded]", meta)

    client.on_dataset(lambda info: asyncio.create_task(downloader(info)))

    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
