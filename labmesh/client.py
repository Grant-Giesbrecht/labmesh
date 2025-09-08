
from __future__ import annotations

import asyncio, os, uuid, pathlib
from typing import Any, Dict, Callable, Awaitable, Optional

import zmq, zmq.asyncio

from labmesh.util import dumps, loads
from labmesh.util import ensure_windows_selector_loop
ensure_windows_selector_loop()


# BROKER_RPC = os.environ.get("LMH_RPC_CONNECT", "tcp://BROKER:5750") # Broker's port that you connect to to say hello or run pings (?)
# BROKER_XPUB = os.environ.get("LMH_XPUB_CONNECT", "tcp://BROKER:5752")

def _curve_client_setup(sock: zmq.Socket):
	""" Sets up the socket `sock` with CURVE encryption.
	"""
	
	csec = os.environ.get("ZMQ_CLIENT_SECRETKEY")
	cpub = os.environ.get("ZMQ_CLIENT_PUBLICKEY")
	spub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if csec and cpub and spub:
		sock.curve_secretkey = csec
		sock.curve_publickey = cpub
		sock.curve_serverkey = spub

class RelayClient:
	""" Agent class to talk to a specific relay.
	"""
	
	def __init__(self, rpc_endpoint: str, *, contex: Optional[zmq.asyncio.Context]=None):
		self.contex = contex or zmq.asyncio.Context.instance()
		self.rpc_endpoint = rpc_endpoint
		self.req: Optional[zmq.asyncio.Socket] = None

	async def connect(self):
		""" Connect socket to relay's RPC endpoint"""
		
		req = self.contex.socket(zmq.DEALER)
		_curve_client_setup(req)
		req.connect(self.rpc_endpoint)
		self.req = req

	async def call(self, method: str, params: Any | None = None, timeout: float = 10.0) -> Any:
		""" Perform a remote procedure call on the target relay. """
		
		# Ensure socket is ready
		assert self.req is not None
		
		# Create a unique ID for the RPC
		rpc_uuid = uuid.uuid4().hex
		
		# Send message packet
		await self.req.send(dumps({"type":"rpc","rpc_uuid":rpc_uuid,"method":method,"params":params}))
		
		# Continue looping until the appropriate return message is received
		while True:
			
			# Read a message (could be a response to a different  RPC)
			msg = loads(await asyncio.wait_for(self.req.recv(), timeout=timeout))
			
			# Check if UUID matches
			if msg.get("rpc_uuid") != rpc_uuid:
				continue
			
			# Ensure appropriate type
			if msg.get("type") == "rpc_result":
				return msg.get("result")
			
			# Check for errors
			if msg.get("type") == "rpc_error":
				err = msg.get("error") or {}
				raise RuntimeError(f"RPC error {err.get('code')}: {err.get('message')}")

	def __getattr__(self, name: str):
		async def _caller(*args, **kwargs):
			params = kwargs if kwargs else list(args) if args else {}
			return await self.call(name, params)
		return _caller

class BankClient:
	def __init__(self, retrieve_endpoint: str, *, contex: Optional[zmq.asyncio.Context]=None):
		self.contex = contex or zmq.asyncio.Context.instance()
		self.retrieve_endpoint = retrieve_endpoint
		self.req: Optional[zmq.asyncio.Socket] = None

	async def connect(self):
		""" Connect socket to the bank's socket."""
		
		req = self.contex.socket(zmq.DEALER)
		_curve_client_setup(req)
		req.connect(self.retrieve_endpoint)
		self.req = req

	async def download(self, dataset_id: str, dest_path: str, *, chunk_cb: Optional[Callable[[int], None]]=None, timeout: float = 60.0) -> Dict[str, Any]:
		assert self.req is not None
		await self.req.send(dumps({"type":"get","dataset_id": dataset_id}))
		meta = loads(await asyncio.wait_for(self.req.recv(), timeout=timeout))
		if meta.get("type") != "meta":
			raise RuntimeError(f"unexpected: {meta}")
		size = meta.get("size"); sha = meta.get("sha256")
		p = pathlib.Path(dest_path)
		with open(p, "wb") as f:
			while True:
				frames = await asyncio.wait_for(self.req.recv_multipart(), timeout=timeout)
				hdr = loads(frames[0])
				if hdr.get("type") != "chunk":
					raise RuntimeError("expected chunk")
				if len(frames) > 1 and frames[1]:
					f.write(frames[1])
					if chunk_cb: chunk_cb(len(frames[1]))
				if hdr.get("eof"):
					break
		return {"dataset_id": dataset_id, "size": size, "sha256": sha, "path": str(p)}

class DirectorClientAgent:
	""" Class that is used by a Director node to manage connections to various worker nodes. The key
	functions are `driver` and `bank` which provide RelayClientAgent and BankClientAgent objects 
	which directly access those objects. NOTE: These functions should be renamed to something
	like get_bank_agent() or get_relay_agent()`.
	"""
	
	def __init__(self, broker_address:str, broker_rpc:str, broker_xpub:str):
		
		self.broker_address = broker_address
		
		self.broker_rpc = broker_rpc.replace("BROKER", self.broker_address)
		self.broker_xpub = broker_xpub.replace("BROKER", self.broker_address)
		
		# Create a ZMQ context
		self.contex = zmq.asyncio.Context.instance()
		
		self.dir_req: Optional[zmq.asyncio.Socket] = None
		self.sub: Optional[zmq.asyncio.Socket] = None
		self._state_callback: list[Callable[[str, Dict[str, Any]], Awaitable[None] | None]] = []
		self._dataset_callback: list[Callable[[Dict[str, Any]], Awaitable[None] | None]] = []

	async def connect(self):
		""" Connect all sockets. This includes the broker's RPC socket for directory requests,
		and the broker's subscriber socket for all subscription packets."""
		
		# Prepare broker RPC socket
		req = self.contex.socket(zmq.DEALER)
		_curve_client_setup(req)
		req.connect(self.broker_rpc)
		self.dir_req = req
		
		# Prepare subscription socket
		sub = self.contex.socket(zmq.SUB)
		_curve_client_setup(sub)
		sub.connect(self.broker_xpub)
		self.sub = sub
		
		# Register with the broker
		await req.send(dumps({"type":"hello","role":"client"}))
		_ = await req.recv() #TODO: Do things with the return data and ensure the connection was successful
		
		# Begin the listener for publish and dataset events
		asyncio.create_task(self._event_listener())

	async def _event_listener(self):
		""" Main function for the event listener coprocess. Sets the subscription
		options for the subscription socket and processes the data as requested
		(specified via callback functions)
		"""
		
		# Ensure the subscriber socket is ready
		assert self.sub is not None
		
		# subscribe to both 'state.' and 'dataset.' prefixes
		self.sub.setsockopt(zmq.SUBSCRIBE, b"state.")
		self.sub.setsockopt(zmq.SUBSCRIBE, b"dataset.")
		
		# Main loop
		while True:
			
			# Wait for subscription data
			topic_enc, payload = await self.sub.recv_multipart()
			topic = topic_enc.decode()
			msg = loads(payload) # Get dict from byte array
			
			# Process state subscriptions
			if topic.startswith("state."):
				
				# Get the data
				rid = msg.get("relay_id")
				st = msg.get("state")
				
				# For each callback function
				for cb_func in list(self._state_callback):
					
					# Call callback
					result = cb_func(rid, st)
					
					# Await result if result is a corroutine (async)
					if asyncio.iscoroutine(result):
						await result
					
			# Process dataset subscriptions
			elif topic.startswith("dataset."):
				
				# For each callback function
				for cb_func in list(self._dataset_callback):
					
					# Call callback
					result = cb_func(msg)
					
					# Await result if result is a coroutine
					if asyncio.iscoroutine(result):
						await result

	def on_state(self, cb: Callable[[str, Dict[str, Any]], Awaitable[None] | None]):
		""" Add a callback function to be executed when a new state subscription
		packet arrives.
		"""
		
		self._state_callback.append(cb)

	def on_dataset(self, cb: Callable[[Dict[str, Any]], Awaitable[None] | None]):
		""" Add a callback function to be executed when a new data subscription
		packet arrives.
		"""
		
		self._dataset_callback.append(cb)

	async def _rpc(self, method: str, params: Dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
		""" Initiate a remote procedure call. 
		"""
		
		# Ensure that the socket for the broker's RPC socket is ready
		assert self.dir_req is not None
		
		# Create a unique ID for the RPC packet
		rpc_uuid = uuid.uuid4().hex
		
		# Send packet
		await self.dir_req.send(dumps({"type":"rpc","rpc_uuid":rpc_uuid,"method":method,"params":params or {}}))
		
		# Continue reading messages until the message with the correct UUID returns
		while True:
			
			# Wait for message
			msg = loads(await asyncio.wait_for(self.dir_req.recv(), timeout=timeout))
			
			# Check for UUID
			if msg.get("rpc_uuid") == rpc_uuid:
				
				# If everything matches up, great, return it out of the function!
				if msg.get("type") == "rpc_result":
					return msg.get("result")
				
				# If UUID matches but type does not, time to freak out a little
				raise RuntimeError(msg.get("error"))
	
	async def list_relay_ids(self) -> list[Dict[str, str]]:
		""" Queries the broker's RPC port to get a dictionary of relay_id:endpoint ."""
		return await self._rpc("list_relay_ids")

	async def list_banks(self) -> list[Dict[str, str]]:
		""" Queries the broker's RPC port to get a dictionary of bank_id:{ingest:, retreive:} ."""
		return await self._rpc("list_banks")

	async def get_relay_agent(self, relay_id: str) -> RelayClient:
		""" Returns a relay client for the specified global name. Raises exception 
		if not found. """
		
		# Get list of global names
		relay_ids = await self.list_relay_ids()
		
		# Get the first endpoint matching the specified name
		ep = next((s["rpc_endpoint"] for s in relay_ids if s["relay_id"] == relay_id), None)
		if not ep:
			raise RuntimeError(f"relay_id '{relay_id}' not found")
		
		# Create a RelayClient for that endpoint
		dc = RelayClient(ep, contex=self.contex)
		await dc.connect()
		
		# Return RelayClient
		return dc

	async def get_databank_agent(self, bank_id: str | None = None) -> BankClient:
		""" Returns a DatabankClientAgent for the specified bank ID. Raises an
		error if none are present. If `bank_id` is None, returns first bank
		to have registered with the broker.
		"""
		
		# Get a list of banks and their ports
		banks = await self.list_banks()
		
		# If banks is empty, raise an error
		if not banks:
			raise RuntimeError("no banks registered")
		
		# If a bank ID is provided, look for it
		if bank_id:
			
			# 
			info = next((b for b in banks if b["bank_id"] == bank_id), None)
			if not info:
				raise RuntimeError(f"bank '{bank_id}' not found")
		else: #Otherwise select first bank
			info = banks[0]
		
		# Create BankClient
		bc = BankClient(info["retrieve"], contex=self.contex)
		
		# Connect client sockets and return object
		await bc.connect()
		return bc
