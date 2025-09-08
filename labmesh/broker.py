
from __future__ import annotations

import asyncio, os
from typing import Dict, Any, Optional

import zmq, zmq.asyncio

from labmesh.util import dumps, loads, ensure_windows_selector_loop
ensure_windows_selector_loop()
# read_toml_config()

# Ports to listen to (which is why no address is specified, just port #)
# RPC_BIND = os.environ.get("LMH_RPC_BIND", "tcp://*:5750")
# XSUB_BIND = os.environ.get("LMH_XSUB_BIND", "tcp://*:5751")
# XPUB_BIND = os.environ.get("LMH_XPUB_BIND", "tcp://*:5752")

def _curve_server_setup(sock: zmq.Socket):
	sec = os.environ.get("ZMQ_SERVER_SECRETKEY")
	pub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if sec and pub:
		sock.curve_secretkey = sec
		sock.curve_publickey = pub
		sock.curve_server = True

class DirectoryBroker:
	"""Presence + endpoint directory + XPUB/XSUB forwarder (no RPC routing)."""
	
	# def __init__(self, *, rpc_bind:str=RPC_BIND, xsub_bind:str=XSUB_BIND, xpub_bind:str=XPUB_BIND):
	def __init__(self, rpc_bind:str, xsub_bind:str, xpub_bind:str):
		
		# Create a ZMQ context
		self.contex = zmq.asyncio.Context.instance()
		
		# Get the rpc, xsub, and xpub socket addresses
		self.rpc_bind = rpc_bind
		self.xsub_bind = xsub_bind
		self.xpub_bind = xpub_bind
		
		# Class variables that will contain the sockets 
		self._router: Optional[zmq.asyncio.Socket] = None # Socket that receives and replies to RPC commands (general commands to the server like ping, get list of relays)
		self._xsub: Optional[zmq.asyncio.Socket] = None # Subscriber type socket that receives from the publishers on the network
		self._xpub: Optional[zmq.asyncio.Socket] = None # Publisher type socket that sends packets from _xsub along to all subscribers on the network
		
		# List of available relays
		self.relays: Dict[str, Dict[str, Any]] = {}   # relay_id -> {rpc_endpoint: str}
		
		# List of available banks
		self.banks: Dict[str, Dict[str, Any]] = {}     # bank_id -> {ingest:str, retrieve:str}
	
	async def _run_state_proxy(self):
		""" Main loop to route subscription packets from publishers to subscribers
		
		TODO: I think this can be made zero-broker in the future.
		"""
		
		# Prepare the subscriber socket
		xsub = self.contex.socket(zmq.XSUB)
		_curve_server_setup(xsub)
		xsub.sndhwm = 4000 # Set send a receive high-water marks
		xsub.rcvhwm = 4000
		xsub.bind(self.xsub_bind)
		
		# Prepare the publisher socket
		xpub = self.contex.socket(zmq.XPUB)
		_curve_server_setup(xpub)
		xpub.sndhwm = 4000
		xpub.rcvhwm = 4000
		xpub.setsockopt(zmq.XPUB_VERBOSE, 1) # XPUB receives subscribe/unsubscribe control frames from SUB clients
		xpub.bind(self.xpub_bind)
		
		# Update class variable and print message
		self._xsub, self._xpub = xsub, xpub
		print(f"[broker] state proxy XSUB={self.xsub_bind} <-> XPUB={self.xpub_bind}")
		
		# Create a poller and tell it to monitor both ports. It creates a future
		# and will only pass `await` when 1+ events are fully ready to be read.
		poller = zmq.asyncio.Poller()
		poller.register(xsub, zmq.POLLIN)
		poller.register(xpub, zmq.POLLIN)
		
		# Main loop
		try:
			while True:
				
				# Aait poller and get a dict of events
				events = dict(await poller.poll())
				
				# Proces events from relays (subscriber pub connected to relay's publisher) SUB -> PUB (publish port connected to client's sub)
				if xsub in events and events[xsub] & zmq.POLLIN: # Forward data frames from relays (PUB) to clients (SUB)
					msg = await xsub.recv_multipart()
					await xpub.send_multipart(msg)
				
				# Proces events PUB -> SUB
				if xpub in events and events[xpub] & zmq.POLLIN: # Forward subscription control frames from clients to XSUB
					sub_msg = await xpub.recv_multipart()
					await xsub.send_multipart(sub_msg)
		finally:
			xsub.close(0)
			xpub.close(0)

	async def _run_directory(self):
		""" Runs the directory thread. 
		"""
		
		# Create the primary intake socket and configure (RPC style)
		router = self.contex.socket(zmq.ROUTER)
		_curve_server_setup(router)
		router.sndhwm = 1000
		router.rcvhwm = 1000
		router.bind(self.rpc_bind)
		self._router = router
		print(f"[broker] directory RPC at {self.rpc_bind}")
		
		# Main loop
		while True:
			
			# Wait for data on socket
			ident, payload = await router.recv_multipart()
			msg = loads(payload)
			msg_type = msg.get("type")
			
			# Process `intake` messages
			if msg_type == "hello":
				
				role = msg.get("role")
				if role == "relay": # Process incoming relay nodes
					
					# Get name and endpoint (I think the endpoint is the broker's address and port) - return error if not provided
					rid, ep = msg.get("relay_id"), msg.get("rpc_endpoint")
					if not rid or not ep:
						await router.send_multipart([ident, dumps({"type":"error","error":{"code":400,"message":"missing relay_id/rpc_endpoint"}})]); continue
					
					# Add to list of relays (key=relay_id), value=dict, specifying only endpoint (currently)
					self.relays[rid] = {"rpc_endpoint": ep}
					
					# Send reply
					await router.send_multipart([ident, dumps({"type":"hello","ok":True,"role":"relay"})])
					
					# Print message
					print(f"[broker] relay online. relay_id:{rid} -> endpoint:{ep}")
					
				elif role == "bank": # Process incoming databank nodes
					
					# Get bank_id, ingest and retreive address+ports
					bank_id = msg.get("bank_id") or "bank"
					ingest, retrieve = msg.get("ingest"), msg.get("retrieve")
					
					# Update bank dict, key=bank_id, value=dict with ingest and retreive addresses+ports.
					self.banks[bank_id] = {"ingest": ingest, "retrieve": retrieve}
					
					# Send and print acknowledgement
					await router.send_multipart([ident, dumps({"type":"hello","ok":True,"role":"bank","bank_id":bank_id})])
					print(f"[broker] bank online. bank_id:{bank_id} ingest={ingest} retrieve={retrieve}")
				else:  # client
					
					#TODO: currently no list of clients is kept. I'll want to change
					# This eventually so the admin can kick some.
					
					# Otherwise assume type = client, send and print ack
					await router.send_multipart([ident, dumps({"type":"hello","ok":True,"role":"client"})])
					print("[broker] client hello")
				continue
			
			# Process `rpc` messages
			elif msg_type == "rpc":
				
				# Get the action to perform and the rpc uuid
				method = msg.get("method")
				uuid = msg.get("rpc_uuid")
				
				# Perform each action
				if method == "list_relay_ids":
					print(f"Received LIST_RELAY_IDs")
					result = [{"relay_id": s, **info} for s, info in sorted(self.relays.items())] # Prepare dict
					await router.send_multipart([ident, dumps({"type":"rpc_result","rpc_uuid":uuid,"result":result})]) # Send response to specific UUID
				elif method == "list_banks":
					print(f"Received LIST_BANKS")
					result = [{"bank_id": b, **info} for b, info in sorted(self.banks.items())]
					await router.send_multipart([ident, dumps({"type":"rpc_result","rpc_uuid":uuid,"result":result})])
				elif method == "ping":
					await router.send_multipart([ident, dumps({"type":"rpc_result","rpc_uuid":uuid,"result":{"ok":True}})])
				else:
					await router.send_multipart([ident, dumps({"type":"rpc_error","rpc_uuid":uuid,"error":{"code":400,"message":f"unknown method {method}"}})])
				continue
	
	async def serve(self):
		# Master function that launches all processes
		
		await asyncio.gather(self._run_state_proxy(), self._run_directory())

async def _main():
	broker = DirectoryBroker()
	await broker.serve()

if __name__ == "__main__":
	import asyncio
	asyncio.run(_main())
