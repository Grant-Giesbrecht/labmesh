
import asyncio, os
from labmesh import DirectorClientAgent, RelayClient
from util import read_toml_config

# Create a parser
parser = argparse.ArgumentParser()
p.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
args = p.parse_args(argv)

# Read TOML file
toml_data = read_toml_config("labmesh.toml")

class PSUClient(RelayClient):
	""" Dummy class for PSU to operate on client side.
	"""
	
	async def set_voltage(self, *, value: float):
		return await super().call("set_voltage", {"value": value})
	async def set_output(self, *, on: bool):
		return await super().call("set_output", {"on": on})
	async def get_state(self):
		return await super().call("get_state", {})

async def main():
	
	# Create a direct agent. This will create agent objects to
	# talk to specific bank and relay objects.
	client = DirectorClientAgent(broker_address=toml_data['broker']['address'], broker_rpc=toml_data['client']['broker_rpc'], broker_xpub=toml['client']['broker_xpub'])
	await client.connect()
	
	# Print overview of all available services
	print("Services:", await client.list_relay_ids())
	print("Banks:", await client.list_banks())
	
	# Get the Relay agent for the specified relay-id
	psu = await client.get_relay_agent("psu-1")
	
	# Send a command to the remote instrument
	await psu.set_voltage(value=2.5)
	
	# auto-pick first bank and download datasets as they appear
	async def downloader(info):
		
		# Get databank client
		bank = await client.get_databank_agent(info.get("bank_id"))
		
		# Get destination filename
		dest = f"./download_{info['dataset_id']}.bin"
		
		# Download data from databank
		meta = await bank.download(info["dataset_id"], dest)
		
		# Print success message
		print("[downloaded]", meta)
	
	# Add callback function for state changes
	client.on_state(lambda rid, st: print(f"[state] {rid}: {st}"))
	
	# Add callback function for dataset uploads
	client.on_dataset(lambda info: print(f"[dataset] new {info}"))
	client.on_dataset(lambda info: asyncio.create_task(downloader(info)))
	
	# Main loop - this is where a script could run, or a GUI, or whatever else you want
	# the client to do.
	while True:
		await asyncio.sleep(1)

if __name__ == "__main__":
	asyncio.run(main())
