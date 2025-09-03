
from __future__ import annotations

import asyncio, os, time, uuid
from typing import Any, Dict, Mapping, Optional

import zmq, zmq.asyncio

from .util import dumps, loads
from .util import ensure_windows_selector_loop
ensure_windows_selector_loop()

BROKER_RPC = os.environ.get("LMH_RPC_CONNECT", "tcp://127.0.0.1:5750")
BROKER_XSUB = os.environ.get("LMH_XSUB_CONNECT", "tcp://127.0.0.1:5751")

DEFAULT_RPC_BIND = os.environ.get("LMH_DRV_RPC_BIND", "tcp://*:5850")  # each relay will pick/override
STATE_PUB_CONNECT = BROKER_XSUB

def _curve_server_setup(sock: zmq.Socket):
	sec = os.environ.get("ZMQ_SERVER_SECRETKEY")
	pub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if sec and pub:
		sock.curve_secretkey = sec; sock.curve_publickey = pub; sock.curve_server = True

def _curve_client_setup(sock: zmq.Socket):
	csec = os.environ.get("ZMQ_CLIENT_SECRETKEY")
	cpub = os.environ.get("ZMQ_CLIENT_PUBLICKEY")
	spub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if csec and cpub and spub:
		sock.curve_secretkey = csec; sock.curve_publickey = cpub; sock.curve_serverkey = spub

class RelayAgent:
	"""relay-side agent with direct RPC server and brokered events."""
	def __init__(self, global_name: str, relay: Any, *, rpc_bind: str = DEFAULT_RPC_BIND, state_interval: float = 1.0):
		self.global_name = global_name
		self.relay = relay
		self.rpc_bind = rpc_bind
		self.state_interval = state_interval

		self.contex = zmq.asyncio.Context.instance()
		self.router: Optional[zmq.asyncio.Socket] = None  # RPC server (ROUTER)
		self.pub: Optional[zmq.asyncio.Socket] = None     # state PUB
		self.dir_req: Optional[zmq.asyncio.Socket] = None # register with broker

	async def _register(self):
		# connect to broker RPC and say hello with our endpoint
		req = self.contex.socket(zmq.DEALER); _curve_client_setup(req); req.connect(BROKER_RPC)
		self.dir_req = req
		# Try to render bind address for clients (replace * with host)
		rpc_endpoint_public = self.rpc_bind.replace("*", "127.0.0.1")
		await req.send(dumps({"type":"hello","role":"relay","global_name": self.global_name, "rpc_endpoint": rpc_endpoint_public}))
		_ = await req.recv()

	async def _serve_rpc(self):
		r = self.contex.socket(zmq.ROUTER); _curve_server_setup(r); r.bind(self.rpc_bind)
		self.router = r
		print(f"[relay:{self.global_name}] RPC at {self.rpc_bind}")
		while True:
			ident, payload = await r.recv_multipart()
			msg = loads(payload)
			if msg.get("type") != "rpc":
				continue
			rid = msg.get("id"); method = msg.get("method"); params = msg.get("params") or {}
			try:
				if not hasattr(self.relay, method):
					raise AttributeError(f"unknown method: {method}")
				fn = getattr(self.relay, method)
				if isinstance(params, dict):
					res = fn(**params)
				elif isinstance(params, list):
					res = fn(*params)
				else:
					res = fn(params)
				await r.send_multipart([ident, dumps({"type":"rpc_result","id":rid,"result":res})])
			except Exception as e:
				await r.send_multipart([ident, dumps({"type":"rpc_error","id":rid,"error":{"code":500,"message":str(e)}})])

	async def _serve_state(self):
		p = self.contex.socket(zmq.PUB); _curve_client_setup(p); p.connect(STATE_PUB_CONNECT)
		self.pub = p
		topic = f"state.{self.global_name}".encode("utf-8")
		print(f"[relay:{self.global_name}] publishing state to {STATE_PUB_CONNECT} topic={topic.decode()}")
		while True:
			if hasattr(self.relay, "poll"):
				st: Mapping[str, Any] = self.relay.poll()
			else:
				st = {"global_name": self.global_name, "ts": time.time()}
			await p.send_multipart([topic, dumps({"global_name": self.global_name, "state": dict(st)})])
			await asyncio.sleep(self.state_interval)

	async def run(self):
		await asyncio.gather(self._register(), self._serve_rpc(), self._serve_state())

# Helper for dataset upload to bank (from relay code)
async def upload_dataset(bank_ingest_endpoint: str, dataset_bytes: bytes, *, dataset_id: Optional[str]=None, global_name: str = "unknown", meta: Optional[Dict[str, Any]]=None):
	contex = zmq.asyncio.Context.instance()
	dealer = contex.socket(zmq.DEALER); _curve_client_setup(dealer); dealer.connect(bank_ingest_endpoint)
	did = dataset_id or uuid.uuid4().hex
	await dealer.send(dumps({"type":"ingest_start","dataset_id": did, "global_name": global_name, "meta": meta or {}}))
	_ = await dealer.recv()  # ack
	CHUNK = 1_000_000
	for i in range(0, len(dataset_bytes), CHUNK):
		chunk = dataset_bytes[i:i+CHUNK]
		await dealer.send_multipart([dumps({"type":"ingest_chunk","dataset_id": did, "seq": i//CHUNK, "eof": False}), chunk])
	await dealer.send_multipart([dumps({"type":"ingest_chunk","dataset_id": did, "seq": (len(dataset_bytes)+CHUNK-1)//CHUNK, "eof": True})])
	_ = await dealer.recv()  # done
	return did
