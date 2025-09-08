
import asyncio, sys, time, random, os
from typing import Dict, Any
from labmesh import RelayAgent
from labmesh.relay import upload_dataset
from util import read_toml_config

# Create a parser
parser = argparse.ArgumentParser()
p.add_argument("--toml", help="Set TOML configuration file", default="labmesh.toml")
p.add_argument("--relay_id", help="Relay ID to use on the network.", default="Inst-0")
args = p.parse_args(argv)

# Read TOML file
toml_data = read_toml_config("labmesh.toml")

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

async def periodic_upload(relay_id: str):
	# pretend a big result every ~5s
	
	# Get databank ingest address
	#TODO: This address should not be hardcoded
	ingest = os.environ.get("LMH_BANK_INGEST_CONNECT", "tcp://127.0.0.1:5761")
	
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
		print(f"[relay:{relay_id}] uploaded dataset id={did}")
		n += 1

async def main():
	
	# # Make a relay_id
	relay_id =  args.relay_id #sys.argv[1] if len(sys.argv) > 1 else "psu-1"
	# 
	# # Select a port to listen to, each driver needs a unique one
	# rpc_addr = sys.argv[2] if len(sys.argv) > 2 else "tcp://*:5850"
	
	# Create the RelayAgent to connect to the network
	agent = RelayAgent(relay_id, MockPSU(relay_id), broker_rpc=toml_data['relay']['broker_rpc'], state_interval=1.0, rpc_bind=rpc_addr, state_pub=toml_data['relay']['broker_xsub'])
	
	# Launch all tasks (RelayAgent's task's and periodic upload)
	await asyncio.gather(agent.run(), periodic_upload(relay_id))

if __name__ == "__main__":
	
	# Run main function
	asyncio.run(main())
