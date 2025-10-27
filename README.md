<h1 align="center">
<img src="https://github.com/Grant-Giesbrecht/labmesh/blob/main/docs/images/labmesh.png?raw=True" width="500">
</h1><br>

Mesh networking package using ZeroMQ specifically designed for [constellation](https://github.com/Grant-Giesbrecht/constellation).

## How It Works

labmesh is a ZeroMQ-based mesh networking package designed for the lab instrument automation package `constellation-core`.

labmesh defines four types of nodes:
 - `broker`: Each network must have exactly one broker. The broker tracks what nodes are connected to the network, and initializes new nodes by passing address info around.
 - `client`: Client nodes send commands to relay nodes, and receive updates from the PUB/SUB model.
 - `relay`: Relay nodes are the 'worker' nodes that connect to lab instruments and relay commands from one or more clients to one or more physical instruments.
 - `databank`: Databank nodes allow data to be stored and managed by a specific network node. Relays publish data to banks, and clients can query the banks for data results.

## Basic Example

A basic example labmesh program will need to define a node of each of the four types. Network configuration info is provided to the node programs via a `toml` file. In the included basic example, that is the `labmesh.toml` file.

### Example Config File

Here's an example config file for the basic labmesh program. The primary purpose of this file is to specify the ports to bind to, and the address each node is defined on.

``` TOML
# Configuration file for application ports and addresses

# ── Broker ────────────────────────────────────────────────────────────────────
[broker]
rpc_bind  = "tcp://*:5750"
xsub_bind = "tcp://*:5751"   # relays'/banks' PUB connect here
xpub_bind = "tcp://*:5752"   # clients' SUB connect here
ttl_seconds = 30
address = "127.0.0.1"

# ── Relay ─────────────────────────────────────────────────────────────────────
[relay]
default_rpc_bind = "tcp://*:5850"      # Relay RPC server - each relay needs a different port!
#state_interval = 1.0
broker_rpc  = "tcp://BROKER:5750"
broker_xsub = "tcp://BROKER:5751"
heartbeat_seconds = 5
upload_timeout  = 10
upload_retries  = 5
upload_chunk    = 1000000
default_address = "127.0.0.1"

# ── Data bank ─────────────────────────────────────────────────────────────────
[bank]
ingest_bind   = "tcp://*:5761"  # drivers upload here
retrieve_bind = "tcp://*:5762"  # clients download here
default_data_dir = "./bank_data" # Makes most sense for each bank to explicitly set
broker_rpc  = "tcp://BROKER:5750"
broker_xsub = "tcp://BROKER:5751"
heartbeat_seconds = 5
default_address = "127.0.0.1"

# ── Client ────────────────────────────────────────────────────────────────────
[client]
broker_rpc = "tcp://BROKER:5750"
broker_xpub = "tcp://BROKER:5752"
default_address = "127.0.0.1"
```

### Creating the Broker

To initialize a network, we need a broker node to be running. Here's a basic script which reads a config (`toml`) file, and begins the broker.

``` Python
import asyncio
from labmesh import DirectoryBroker
import argparse
from labmesh.util import read_toml_config

# Create a parser
parser = argparse.ArgumentParser()
parser.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
args = parser.parse_args()

# Read TOML file
toml_broker = read_toml_config(args.toml)['broker']

if __name__ == "__main__":
    broker = DirectoryBroker(toml_broker['rpc_bind'], toml_broker['xsub_bind'], toml_broker['xpub_bind'])
    asyncio.run(broker.serve())
```

### Creating a Relay

This relay node reads the `toml` config, allows a custom RPC port number to be given *(Is a custom RPC mandatory for each relay?)*. The periodic upload function mimics uploading data to a databank periodically.

``` Python
import asyncio, sys, time, random, os
from typing import Dict, Any
from labmesh import RelayAgent
from labmesh.relay import upload_dataset
from labmesh.util import read_toml_config
import argparse

# Create a parser
parser = argparse.ArgumentParser()
parser.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
parser.add_argument("--relay_id", help="Relay ID to use on the network.", default="Inst-0")
parser.add_argument("--rpc", help="RPC port", default="")
args = parser.parse_args()

# Read TOML file
toml_data = read_toml_config(args.toml)

class MockPSU:
	""" Dummy class to pretend to be a power supply unit. """
	
	def __init__(self, relay_id: str):
		self.rid = relay_id
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
		return {"relay_id": self.rid, "voltage": self.voltage, "current": self.current,
				"output": self.output, "last_updated": self.last_updated, "runs": self._count}

	def poll(self) -> Dict[str, Any]:
		self.current = round(self.voltage * (0.9 + 0.2 * random.random()), 4)
		return self.get_state()

async def periodic_upload(relay_id: str, ingest:str):
	# pretend a big result every ~5s
	
	# Main loop
	n = 0
	while True:
		
		# Pause...
		await asyncio.sleep(60)
		
		# Create a fake data payload
		payload = ("Result %d from %s\n" % (n, relay_id)).encode() * 200000  # ~4MB
		
		# Upload, get dataset_id
		did = await upload_dataset(ingest, payload, relay_id=relay_id, meta={"note":"demo"})
		
		# Print confirmation
		print(f"[relay:{relay_id}] uploaded dataset id={did} to {ingest}")
		n += 1

async def main():
	
	# # Make a relay_id
	relay_id =  args.relay_id #sys.argv[1] if len(sys.argv) > 1 else "psu-1"
	# 
	# # Select a port to listen to, each driver needs a unique one
	rpc_addr = args.rpc #sys.argv[2] if len(sys.argv) > 2 else "tcp://*:5850"
	if rpc_addr == "":
		rpc_addr = toml_data['relay']['default_rpc_bind']
	
	# Create the RelayAgent to connect to the network
	agent = RelayAgent(relay_id, MockPSU(relay_id), broker_rpc=toml_data['relay']['broker_rpc'], state_interval=1.0, rpc_bind=rpc_addr, state_pub=toml_data['relay']['broker_xsub'], local_address=toml_data['relay']['default_address'], broker_address=toml_data['broker']['address'])
	
	# Get databank ingest address
	#TODO: This address should not be hardcoded
	# ingest = os.environ.get("LMH_BANK_INGEST_CONNECT", "tcp://127.0.0.1:5761")
	ingest = toml_data['bank']['ingest_bind'].replace("*", toml_data['bank']['default_address'])
	
	# Launch all tasks (RelayAgent's task's and periodic upload)
	await asyncio.gather(agent.run(), periodic_upload(relay_id, ingest))

if __name__ == "__main__":
	
	# Run main function
	asyncio.run(main())

```

### Creating a client

This client script is somewhat more complex. It loads the same config file, then creates a `DirectorClientAgent` called `client`. `client` uses `on_state` and `on_dataset` to connect callbacks for when new instrument states or new datasets are published on the network. It also creates an agent, `psu`, to access the instrument named `Inst-0`. This `psu` object can then be used to remotely access that instrument.

``` Python
import asyncio, os
from labmesh import DirectorClientAgent, RelayClient
from labmesh.util import read_toml_config
import argparse

# Create a parser
parser = argparse.ArgumentParser()
parser.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
args = parser.parse_args()

# Read TOML file
toml_data = read_toml_config(args.toml)

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
	client = DirectorClientAgent(broker_address=toml_data['broker']['address'], broker_rpc=toml_data['client']['broker_rpc'], broker_xpub=toml_data['client']['broker_xpub']) #, local_address=toml_data['client']['default_address'])
	await client.connect()
	
	# Print overview of all available services
	print("Services:", await client.list_relay_ids())
	print("Banks:", await client.list_banks())
	
	# Get the Relay agent for the specified relay-id
	psu = await client.get_relay_agent("Inst-0")
	
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
```

### Creating a Databank

The databank, similar to the broker, also has little to do outside of reading a config script.

``` Python

import asyncio
from labmesh import DataBank
from labmesh.util import read_toml_config
import argparse

# Create a parser
parser = argparse.ArgumentParser()
parser.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
parser.add_argument("--bank_id", help="Bank ID to use on the network.", default="bank-0")
args = parser.parse_args()

# Read TOML file
toml_data = read_toml_config(args.toml)
toml_bank = toml_data['bank']

if __name__ == "__main__":
	bank = DataBank(ingest_bind=toml_bank['ingest_bind'], retrieve_bind=toml_bank['retrieve_bind'], data_dir=toml_bank['default_data_dir'], broker_rpc=toml_bank['broker_rpc'], broker_xsub=toml_bank['broker_xsub'], bank_id=args.bank_id, heartbeat_sec=toml_bank['heartbeat_seconds'], broker_address=toml_data['broker']['address'], local_address=toml_bank['default_address'])
	asyncio.run(bank.serve())
```

