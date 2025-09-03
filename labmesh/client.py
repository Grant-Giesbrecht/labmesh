
from __future__ import annotations

import asyncio, os, uuid, pathlib
from typing import Any, Dict, Callable, Awaitable, Optional

import zmq, zmq.asyncio

from .util import dumps, loads
from .util import ensure_windows_selector_loop
ensure_windows_selector_loop()


BROKER_RPC = os.environ.get("LMH_RPC_CONNECT", "tcp://127.0.0.1:5750") # Broker's port that you connect to to say hello or run pings (?)
BROKER_XPUB = os.environ.get("LMH_XPUB_CONNECT", "tcp://127.0.0.1:5752")

def _curve_client_setup(sock: zmq.Socket):
	csec = os.environ.get("ZMQ_CLIENT_SECRETKEY")
	cpub = os.environ.get("ZMQ_CLIENT_PUBLICKEY")
	spub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if csec and cpub and spub:
		sock.curve_secretkey = csec; sock.curve_publickey = cpub; sock.curve_serverkey = spub

class RelayClient:
	def __init__(self, rpc_endpoint: str, *, contex: Optional[zmq.asyncio.Context]=None):
		self.contex = contex or zmq.asyncio.Context.instance()
		self.rpc_endpoint = rpc_endpoint
		self.req: Optional[zmq.asyncio.Socket] = None

	async def connect(self):
		req = self.contex.socket(zmq.DEALER); _curve_client_setup(req); req.connect(self.rpc_endpoint); self.req = req

	async def call(self, method: str, params: Any | None = None, timeout: float = 10.0) -> Any:
		assert self.req is not None
		rpc_uuid = uuid.uuid4().hex
		await self.req.send(dumps({"type":"rpc","rpc_uuid":rpc_uuid,"method":method,"params":params}))
		while True:
			msg = loads(await asyncio.wait_for(self.req.recv(), timeout=timeout))
			if msg.get("rpc_uuid") != rpc_uuid:
				continue
			if msg.get("type") == "rpc_result":
				return msg.get("result")
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
		req = self.contex.socket(zmq.DEALER); _curve_client_setup(req); req.connect(self.retrieve_endpoint); self.req = req

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

class LabClient:
	def __init__(self):
		self.contex = zmq.asyncio.Context.instance()
		self.dir_req: Optional[zmq.asyncio.Socket] = None
		self.sub: Optional[zmq.asyncio.Socket] = None
		self._state_cbs: list[Callable[[str, Dict[str, Any]], Awaitable[None] | None]] = []
		self._dataset_cbs: list[Callable[[Dict[str, Any]], Awaitable[None] | None]] = []

	async def connect(self):
		req = self.contex.socket(zmq.DEALER); _curve_client_setup(req); req.connect(BROKER_RPC); self.dir_req = req
		sub = self.contex.socket(zmq.SUB); _curve_client_setup(sub); sub.connect(BROKER_XPUB); self.sub = sub
		# hello
		await req.send(dumps({"type":"hello","role":"client"})); _ = await req.recv()
		# start listener
		asyncio.create_task(self._event_listener())

	async def _event_listener(self):
		assert self.sub is not None
		# subscribe to both 'state.' and 'dataset.' prefixes
		self.sub.setsockopt(zmq.SUBSCRIBE, b"state.")
		self.sub.setsockopt(zmq.SUBSCRIBE, b"dataset.")
		while True:
			topic, payload = await self.sub.recv_multipart()
			t = topic.decode()
			msg = loads(payload)
			if t.startswith("state."):
				rid = msg.get("relay_id"); st = msg.get("state")
				for cb in list(self._state_cbs):
					res = cb(rid, st)
					if asyncio.iscoroutine(res): await res
			elif t.startswith("dataset."):
				for cb in list(self._dataset_cbs):
					res = cb(msg)
					if asyncio.iscoroutine(res): await res

	def on_state(self, cb: Callable[[str, Dict[str, Any]], Awaitable[None] | None]):
		self._state_cbs.append(cb)

	def on_dataset(self, cb: Callable[[Dict[str, Any]], Awaitable[None] | None]):
		self._dataset_cbs.append(cb)

	async def _rpc(self, method: str, params: Dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
		assert self.dir_req is not None
		rpc_uuid = uuid.uuid4().hex
		await self.dir_req.send(dumps({"type":"rpc","rpc_uuid":rpc_uuid,"method":method,"params":params or {}}))
		
		# Continue reading messages until the message with the correct UUID returns
		while True:
			
			# Wait for message
			msg = loads(await syncio.wait_for(self.dir_req.recv(), timeout=timeout))
			
			# Check for UUID
			if msg.get("rpc_uuid") == rpc_uuid:
				
				# If everything matches up, great, return it out of the function!
				if msg.get("type") == "rpc_result":
					return msg.get("result")
				
				# If UUID matches but type does not, time to freak out a little
				raise RuntimeError(msg.get("error"))
	
	#TODO: I think only Relays get relay_ids and I could rename list_gloabl_names get_relays or something
	async def list_relay_ids(self) -> list[Dict[str, str]]:
		""" Queries the broker's RPC port to get a dictionary of relay_id:endpoint ."""
		return await self._rpc("list_relay_ids")

	async def list_banks(self) -> list[Dict[str, str]]:
		return await self._rpc("list_banks")

	async def relay(self, relay_id: str) -> RelayClient:
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

	async def bank(self, bank_id: str | None = None) -> BankClient:
		"""
		"""
		
		banks = await self.list_banks()
		if not banks:
			raise RuntimeError("no banks registered")
		if bank_id:
			info = next((b for b in banks if b["bank_id"] == bank_id), None)
			if not info:
				raise RuntimeError(f"bank '{bank_id}' not found")
		else:
			info = banks[0]
		bc = BankClient(info["retrieve"], contex=self.contex)
		await bc.connect()
		return bc
