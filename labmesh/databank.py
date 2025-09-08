from __future__ import annotations

import asyncio, os, uuid, time, pathlib, hashlib
from typing import Dict, Any, Optional, Tuple

import zmq, zmq.asyncio

from labmesh.util import dumps, loads
from labmesh.util import ensure_windows_selector_loop
ensure_windows_selector_loop()

# BROKER_RPC = os.environ.get("LMH_RPC_CONNECT", "tcp://BROKER:5750")
# BROKER_XSUB = os.environ.get("LMH_XSUB_CONNECT", "tcp://BROKER:5751")

# DEFAULT_INGEST_BIND = os.environ.get("LMH_BANK_INGEST_BIND", "tcp://*:5761")
# DEFAULT_RETRIEVE_BIND = os.environ.get("LMH_BANK_RETRIEVE_BIND", "tcp://*:5762")
# DATA_DIR = os.environ.get("LMH_BANK_DATA_DIR", "./bank_data")
# BANK_ID = os.environ.get("LMH_BANK_ID", "bank-1")
CHUNK_SIZE = 1_000_000  # 1 MB
HEARTBEAT_SEC = int(os.environ.get("LMH_HEARTBEAT_SECONDS", "5"))

def _curve_server_setup(sock: zmq.Socket):
	#TODO: Document requirements: One set of secret + public server keys, 1 or N for clients?
	
	sec = os.environ.get("ZMQ_SERVER_SECRETKEY")
	pub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if sec and pub:
		sock.curve_secretkey = sec; sock.curve_publickey = pub; sock.curve_server = True

def _curve_client_setup(sock: zmq.Socket):
	# TODO: Document key requirements
	
	csec = os.environ.get("ZMQ_CLIENT_SECRETKEY")
	cpub = os.environ.get("ZMQ_CLIENT_PUBLICKEY")
	spub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if csec and cpub and spub:
		sock.curve_secretkey = csec; sock.curve_publickey = cpub; sock.curve_serverkey = spub

class DataBank:
	"""Accepts dataset uploads and serves downloads (with checksum verification).

	Upload protocol (relay -> bank):
	  - ingest_start: {dataset_id, relay_id, meta, size, sha256}
	  - ingest_chunk: {dataset_id, seq, eof:false} + [binary chunk]  -> bank replies {ingest_ack_chunk, next_seq}
	  - ingest_chunk eof:true (no chunk) -> bank verifies (size+sha256), announces dataset, replies ingest_done

	Download protocol (client -> bank):
	  - get: {dataset_id} -> meta + chunk stream
	"""
	
	def __init__(self, *, ingest_bind:str, retrieve_bind:str, data_dir:str, broker_rpc:str, broker_xsub:str, bank_id:str, heartbeat_sec:int=5, local_address:str="127.0.0.1", broker_address:str="127.0.0.1" ):
		
		# Create ZMQ context
		self.contex = zmq.asyncio.Context.instance()
		
		self.local_address = local_address
		self.broker_addess = broker_address
		self.heartbeat_sec = heartbeat_sec
		
		# List socket addresses
		self.ingest_bind = ingest_bind # ZMQ address - data I/O
		self.retrieve_bind = retrieve_bind # ZMQ address - data I/O
		self.broker_xsub = broker_xsub.replace("BROKER", self.broker_addess) # Broker subscription socket
		self.broker_rpc = broker_rpc.replace("BROKER", self.broker_addess) # Broker RPC socket
		
		# Directory where data is stored 
		self.data_dir = pathlib.Path(data_dir); self.data_dir.mkdir(parents=True, exist_ok=True)
		
		# databank name
		self.bank_id = bank_id
		
		# Associated sockets
		self.ingest_router: Optional[zmq.asyncio.Socket] = None
		self.retrieve_router: Optional[zmq.asyncio.Socket] = None
		self.pub: Optional[zmq.asyncio.Socket] = None
		self.rpc_sock: Optional[zmq.asyncio.Socket] = None  # to register/heartbeat with broker
		
		# simple index
		self.index_path = self.data_dir / "index.json"
		self.index: Dict[str, Dict[str, Any]] = {}
		if self.index_path.exists():
			try:
				self.index = loads(self.index_path.read_bytes())
			except Exception:
				self.index = {}

	async def _register_with_broker(self):
		""" Connect and register with broker. """
		
		# Setup the dealer style -> broker RPC socket
		req = self.contex.socket(zmq.DEALER) # Dealer socket can send and receive
		_curve_client_setup(req)
		req.connect(self.broker_rpc)
		self.rpc_sock = req
		
		# Send hello message to broker, registering the bank
		await req.send(dumps({"type":"hello","role":"bank","bank_id": self.bank_id,
							  "ingest": self.ingest_bind.replace("*",self.local_address),
							  "retrieve": self.retrieve_bind.replace("*",self.local_address)}))
		
		# Receive response
		# TODO: Do something with the acknowledgement
		_ = await req.recv()
		
		# Setup the publisher style -> broker subscriber socket
		pub = self.contex.socket(zmq.PUB)
		_curve_client_setup(pub)
		pub.connect(self.broker_xsub)
		self.pub = pub
		
		# Begin heartbeat task
		asyncio.create_task(self._heartbeat())

	async def _heartbeat(self):
		""" Heartbeat task: Continuously show a sign of life to the broker
		"""
		
		# Ensure that the socket is ready
		assert self.rpc_sock is not None
		
		# Infinite loop
		while True:
			
			# Wait the heartbeat period
			await asyncio.sleep(self.heartbeat_sec)
			
			# Attempt to send a message to the broker
			# TODO: Modify Broker to actually use this 
			try:
				await self.rpc_sock.send(dumps({"type":"heartbeat","role":"bank","bank_id": self.bank_id}))
			except Exception:
				pass

	async def _run_ingest(self):
		""" Coprocess that ingests data as it is sent from relay nodes.
		"""
		
		# Prepare the router style (relay -> ingest) socket
		in_router = self.contex.socket(zmq.ROUTER)
		_curve_server_setup(in_router)
		in_router.bind(self.ingest_bind)
		self.ingest_router = in_router
		
		# Print location of ingest socket
		print(f"[bank] ingest socket at {self.ingest_bind}")
		
		class Session:
			""" Class that defines a session of data being uploaded. """
			
			__slots__ = ("dataset_id","relay_id","meta","file_handle","hasher","next_seq","expected_size","expected_sha")
			def __init__(self, dataset_id, relay_id, meta, file_handle, expected_size, expected_sha):
				self.dataset_id = dataset_id
				self.relay_id = relay_id
				self.meta = meta
				self.file_handle = file_handle
				self.hasher = hashlib.sha256()
				self.next_seq = 0
				self.expected_size = expected_size
				self.expected_sha = expected_sha
		
		# Prepare a dictionary of inflight sessions, these are packets in the process of being downloaded
		# over a series of 1+ packets. 
		#  - First a type:ingest_start is sent, then 1+ type:ingest_chunk:eof=False, then a type:ingest_chunk:eof=True
		inflight: Dict[bytes, Session] = {}
		
		# Main loop
		while True:
			
			# recv_multipart gets all data frames (as bytes) and returns then in one blocking go
			frames = await in_router.recv_multipart()
			
			# The dealer->router pattern in ZMQ will send 2 frames, the identity of the sender, then the actual message sent. This
			# is why you only see the 'payload' being sent in the relay.py's upload function.
			try:
				ident, payload = frames[0], frames[1] # First frame is identity of sender, second frame is the actual payload
			except Exception as e:
				print(f"Exception occured while attempting to read 2 frames from recv_multipart ({e})")
				continue
			
			# Unpack data from payload
			packet_data = loads(payload)
			
			# Get the message type 
			ingest_type = packet_data.get("type")
			
			# Check if it's the first component of the data packet
			if ingest_type == "ingest_start":
				
				# Unpack packet
				dataset_id = packet_data.get("dataset_id") or uuid.uuid4().hex # Get dataset id
				rid = packet_data.get("relay_id") or "unknown" # Get relay id
				meta = packet_data.get("meta") or {} # Get metadata
				
				#TODO: I don't see this in the upload function?
				expected_size = int(packet_data.get("size") or 0)
				expected_sha = packet_data.get("sha256") or ""
				
				# Choose filenmae based on dataset ID
				path = self.data_dir / f"{dataset_id}.bin"
				fh = open(path, "wb") # Open file handle
				
				# Create a session object and add to inflight - you could have multiple from multiple relays
				inflight[ident] = Session(dataset_id, rid, meta, fh, expected_size, expected_sha)
				
				# Send acknowledgement message
				await in_router.send_multipart([ident, dumps({"type":"ingest_ack","dataset_id": dataset_id})])
				continue
			
			# Else, check if it's a follow up chunk
			elif ingest_type == "ingest_chunk":
				
				# Get the session object matching the sender
				sess = inflight.get(ident)
				if not sess:
					
					# Send an error if you can't find the sender
					await in_router.send_multipart([ident, dumps({"type":"error","error":{"code":409,"message":"no ingest_start"}})])
					continue
				
				# 
				seq = int(packet_data.get("seq") or 0) # Get sequence (ie. )
				eof = bool(packet_data.get("eof"))
				chunk = frames[2] if len(frames) > 2 else b""
				
				# Check if this is the last chunk...
				if not eof: # Not last chunk
					
					# If chunk was received out of order, update status
					if seq != sess.next_seq:
						await in_router.send_multipart([ident, dumps({"type":"ingest_ack_chunk","dataset_id": sess.dataset_id, "next_seq": sess.next_seq, "status":"out_of_order"})])
						continue
					
					# Process chunk
					if chunk:
						sess.file_handle.write(chunk) # Write chunk to file
						sess.hasher.update(chunk) # Add chunk to hasher's total data
						sess.next_seq += 1 # update next expected sequence
						
					# Send acknowledgement 
					await in_router.send_multipart([ident, dumps({"type":"ingest_ack_chunk","dataset_id": sess.dataset_id, "next_seq": sess.next_seq})])
					continue
				
				# EOF was received (no more chunk data)
				else:
					
					# Close file
					sess.file_handle.flush()
					sess.file_handle.close()
					
					# Get stats on final result
					path = self.data_dir / f"{sess.dataset_id}.bin"
					size = path.stat().st_size # Get size of file in bytes
					sha = sess.hasher.hexdigest() # Run hash digest
					
					# Run error checks
					ok = True
					err = None
					if sess.expected_size and size != sess.expected_size: # Size mismatch
						ok = False
						err = f"size mismatch: got {size}, expected {sess.expected_size}"
					if sess.expected_sha and sha != sess.expected_sha: # Hash mismatch
						ok = False
						err = f"sha mismatch: got {sha}, expected {sess.expected_sha}"
					if ok: # All okay
						
						# Add to index[dataset_id] = dict of file info
						self.index[sess.dataset_id] = {"path": str(path), "size": size, "sha256": sha, "ts": time.time(), "meta": sess.meta, "relay_id": sess.relay_id}
						
						# Write to index file
						self.index_path.write_bytes(dumps(self.index))
						
						# Ensure that publishing socket is ready
						assert self.pub is not None
						
						# Prepare topic for publishing
						topic = f"dataset.{self.bank_id}".encode("utf-8")
						
						# Publish that a new dataset has been received
						await self.pub.send_multipart([topic, dumps({"dataset_id": sess.dataset_id, "bank_id": self.bank_id, "size": size, "sha256": sha, "relay_id": sess.relay_id})])
						
						# Send ack to source-relay that ingest is successfully completed
						await in_router.send_multipart([ident, dumps({"type":"ingest_done","dataset_id": sess.dataset_id, "size": size, "sha256": sha})])
					else:
						
						# If an error occured, reply to the source relay with that error
						await in_router.send_multipart([ident, dumps({"type":"error","dataset_id": sess.dataset_id, "error":{"code":422,"message":err}})])
					
					# Remove current session from the list of 'inflight' packets
					inflight.pop(ident, None)
					continue

	async def _run_retrieve(self):
		""" Coprocess that returns data to clients when requested.
		"""
		
		# Prepare the router-style socket (databank -> clients)
		r = self.contex.socket(zmq.ROUTER)
		_curve_server_setup(r)
		r.bind(self.retrieve_bind)
		self.retrieve_router = r
		print(f"[bank] retrieve socket at {self.retrieve_bind}")
		
		# Main loop
		while True:
			
			# Get client identity and message payload
			ident, payload = await r.recv_multipart()
			
			# Unpack packet bytes -> dict
			packet_data = loads(payload)
			
			# Ensure the messaage type = 'get'
			if packet_data.get("type") != "get":
				# Send an erorr message back
				await r.send_multipart([ident, dumps({"type":"error","error":{"code":400,"message":"expected get"}})])
				# Resume main loop
				continue
			
			# Else get dataset id
			dataset_id = packet_data.get("dataset_id")
			
			# Lookup index entry of data to return to client
			info = self.index.get(dataset_id)
			
			# If failed to find index data send error
			if not info:
				await r.send_multipart([ident, dumps({"type":"error","error":{"code":404,"message":"not found"}})])
				continue
			
			
			path = info["path"] # Get path to data file
			size = info["size"] # Get size of data file
			sha = info["sha256"] # Get hash
			meta = info.get("meta", {}) # Get metadata
			
			# Send file info, size, etc
			await r.send_multipart([ident, dumps({"type":"meta","dataset_id": dataset_id, "size": size, "sha256": sha, "meta": meta})])
			
			# Open file to send
			with open(path, "rb") as f:
				
				# Initialize sequence counter 
				seq = 0
				
				# Loop until file is sent
				while True:
					
					# Read a chunk... break when no more available
					chunk = f.read(CHUNK_SIZE) 
					if not chunk:
						break
					
					# Send data to client
					await r.send_multipart([ident, dumps({"type":"chunk","dataset_id": dataset_id, "seq": seq, "eof": False}), chunk])
					
					# Increment sequence counter
					seq += 1
				
				# Send EOF signal
				await r.send_multipart([ident, dumps({"type":"chunk","dataset_id": dataset_id, "seq": seq, "eof": True})])

	async def serve(self):
		""" Begin databank process. """
		
		await self._register_with_broker() # NOTE: This also starts the heartbeat process # TODO: Move this into the __init__?
		await asyncio.gather(self._run_ingest(), self._run_retrieve())

async def _main():
	
	bank = DataBank()
	await bank.serve()

if __name__ == "__main__":
	import asyncio
	asyncio.run(_main())
