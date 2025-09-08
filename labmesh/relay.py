
from __future__ import annotations

import asyncio, os, time, uuid
from typing import Any, Dict, Mapping, Optional

import zmq, zmq.asyncio
from labmesh.util import dumps, loads
from labmesh.util import ensure_windows_selector_loop
ensure_windows_selector_loop()


# BROKER_RPC = os.environ.get("LMH_RPC_CONNECT", "tcp://BROKER:5750") # TODO: So the broker is at 127.0.0.1?
BROKER_XSUB = os.environ.get("LMH_XSUB_CONNECT", "tcp://BROKER:5751")

# DEFAULT_RPC_BIND = os.environ.get("LMH_DRV_RPC_BIND", "tcp://*:5850")  # each relay will pick/override
# STATE_PUB_CONNECT = BROKER_XSUB

def _curve_server_setup(sock: zmq.Socket):
	""" Configures CURVE for a sockets connecting to Broker for publishing"""
	sec = os.environ.get("ZMQ_SERVER_SECRETKEY")
	pub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if sec and pub:
		sock.curve_secretkey = sec
		sock.curve_publickey = pub
		sock.curve_server = True

def _curve_client_setup(sock: zmq.Socket):
	
	csec = os.environ.get("ZMQ_CLIENT_SECRETKEY")
	cpub = os.environ.get("ZMQ_CLIENT_PUBLICKEY")
	spub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if csec and cpub and spub:
		sock.curve_secretkey = csec
		sock.curve_publickey = cpub
		sock.curve_serverkey = spub

class RelayAgent:
	"""relay-side agent with direct RPC server and brokered events."""
	
	def __init__(self, relay_id:str, relay:Any, broker_rpc:str, rpc_bind:str, state_pub:str, local_address:str="127.0.0.1", broker_address:str="127.0.0.1",  state_interval:float=1.0, ):
		self.relay_id = relay_id
		self.relay = relay
		self.rpc_bind = rpc_bind
		self.state_interval = state_interval
		self.broker_rpc_raw = broker_rpc
		self.state_pub_raw = state_pub
		
		self.local_address = local_address
		self.broker_addess = broker_address
		
		self.contex = zmq.asyncio.Context.instance()
		self.router: Optional[zmq.asyncio.Socket] = None  # RPC server (ROUTER)
		self.pub: Optional[zmq.asyncio.Socket] = None     # state PUB
		self.dir_req: Optional[zmq.asyncio.Socket] = None # register with broker

	async def _register(self):
		""" Connect to the network by sending a hello to the Broker. """
		
		# connect to broker RPC and say hello with the relay's endpoint
		req = self.contex.socket(zmq.DEALER)
		_curve_client_setup(req)
		broker_rpc_addr = self.broker_rpc_raw.replace("BROKER", self.broker_addess)
		req.connect(broker_rpc_addr)
		self.dir_req = req
		
		# Try to render bind address for clients (replace * with host)
		rpc_endpoint_public = self.rpc_bind.replace("*", self.local_address)
		await req.send(dumps({"type":"hello","role":"relay","relay_id": self.relay_id, "rpc_endpoint": rpc_endpoint_public}))
		
		#TODO: Use ACK message
		_ = await req.recv() 

	async def _serve_rpc(self):
		""" Respond to a RPC call NOTE: from broker or client?
		"""
		
		# Prepare socket
		r = self.contex.socket(zmq.ROUTER)
		_curve_server_setup(r)
		r.bind(self.rpc_bind)
		self.router = r
		print(f"[relay:{self.relay_id}] RPC at {self.rpc_bind}")
		
		# Main loop
		while True:
			
			# Get identity of sender and message payload
			ident, payload = await r.recv_multipart()
			msg = loads(payload) # Decode message
			
			# Check that message type is remote-procedure-call
			if msg.get("type") != "rpc":
				continue
			
			# Unpack message components
			rid = msg.get("rpc_uuid")
			method = msg.get("method")
			params = msg.get("params") or {}
			
			# Attempt to execute
			try:
				
				# Verify that method exists
				if not hasattr(self.relay, method):
					raise AttributeError(f"unknown method: {method}")
				
				# Get method
				fn = getattr(self.relay, method)
				
				# Apply parameters and call method
				if isinstance(params, dict):
					res = fn(**params)
				elif isinstance(params, list):
					res = fn(*params)
				else:
					res = fn(params)
					
				# Send ACK message
				await r.send_multipart([ident, dumps({"type":"rpc_result","rpc_uuid":rid,"result":res})])
			except Exception as e:
				await r.send_multipart([ident, dumps({"type":"rpc_error","rpc_uuid":rid,"error":{"code":500,"message":str(e)}})])

	async def _serve_state(self):
		""" Coroutine to periodically push states to all subscribers.
		
		It periodically calls `self.relay.poll()`. Whatever you need to update the state of
		the object, place that in `poll()`. Expects `poll()` to return a dictionary.
		
		"""
		
		# Create publisher socket
		p = self.contex.socket(zmq.PUB)
		_curve_client_setup(p)
		state_pub_addr = self.state_pub_raw.replace("BROKER", self.broker_addess)
		p.connect(state_pub_addr)
		self.pub = p
		
		# Prepare topic string from relay ID
		topic = f"state.{self.relay_id}".encode("utf-8")
		
		# Print message
		print(f"[relay:{self.relay_id}] periodically publishing state to {state_pub_addr} topic={topic.decode()}")
		
		# Main loop
		while True:
			
			# Ensure object has 'poll' method
			if hasattr(self.relay, "poll"):
				st: Mapping[str, Any] = self.relay.poll() #TODO: Add timestamp 
			else:
				st = {"relay_id": self.relay_id, "ts": time.time()}
			
			# Send packet
			await p.send_multipart([topic, dumps({"relay_id": self.relay_id, "state": dict(st)})])
			
			# Pause for interval
			await asyncio.sleep(self.state_interval)

	async def run(self):
		""" Runs the server by starting all coroutines:
		 * _register: registers the IP address and endpoint with the broker
		 * _serve_rpc: Responds to remote-procedure-calls
		 * _serve_state: Periodically pushes state updates to all subscribers.
		
		"""
		await asyncio.gather(self._register(), self._serve_rpc(), self._serve_state())

# Helper for dataset upload to bank (from relay code)
async def upload_dataset(bank_ingest_endpoint: str, dataset_bytes: bytes, *, dataset_id: Optional[str]=None, relay_id: str = "unknown", meta: Optional[Dict[str, Any]]=None):
	""" Uploads a dataset to a specific band endpoint. 
	
	Returns:
	 	dataset_id
	"""
	
	# Create ZMQ context
	contex = zmq.asyncio.Context.instance()
	
	# Create a dealer socket (to connect to the databank's router socket)
	dealer = contex.socket(zmq.DEALER)
	_curve_client_setup(dealer)
	dealer.connect(bank_ingest_endpoint)
	
	# Get ID for dataset
	did = dataset_id or uuid.uuid4().hex
	
	# Send ingest start
	await dealer.send(dumps({"type":"ingest_start","dataset_id": did, "relay_id": relay_id, "meta": meta or {}}))
	
	# TODO: Do something with the ack message
	_ = await dealer.recv()  # ack
	
	# Pick chunk size
	CHUNK = 1_000_000
	
	# Loop over each chunk until the whole chunk is sent
	for i in range(0, len(dataset_bytes), CHUNK):
		
		# Get chunk from total data
		chunk = dataset_bytes[i:i+CHUNK]
		
		# Send a chunk
		await dealer.send_multipart([dumps({"type":"ingest_chunk","dataset_id": did, "seq": i//CHUNK, "eof": False}), chunk])
		
		#TODO: Why doesn't this ever run receive? Acknowledgements are periodically sent
	
	# Send EOF
	await dealer.send_multipart([dumps({"type":"ingest_chunk","dataset_id": did, "seq": (len(dataset_bytes)+CHUNK-1)//CHUNK, "eof": True})])
	_ = await dealer.recv()  # done
	return did
