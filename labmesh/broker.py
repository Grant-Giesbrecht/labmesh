
from __future__ import annotations

import asyncio, os
from typing import Dict, Any, Optional

import zmq, zmq.asyncio

from .util import dumps, loads
from .util import ensure_windows_selector_loop
ensure_windows_selector_loop()


RPC_BIND = os.environ.get("LMH_RPC_BIND", "tcp://*:5750")
XSUB_BIND = os.environ.get("LMH_XSUB_BIND", "tcp://*:5751")
XPUB_BIND = os.environ.get("LMH_XPUB_BIND", "tcp://*:5752")

def _curve_server_setup(sock: zmq.Socket):
	sec = os.environ.get("ZMQ_SERVER_SECRETKEY")
	pub = os.environ.get("ZMQ_SERVER_PUBLICKEY")
	if sec and pub:
		sock.curve_secretkey = sec
		sock.curve_publickey = pub
		sock.curve_server = True

class DirectoryBroker:
	"""Presence + endpoint directory + XPUB/XSUB forwarder (no RPC routing)."""
	def __init__(self, *, rpc_bind: str = RPC_BIND, xsub_bind: str = XSUB_BIND, xpub_bind: str = XPUB_BIND):
		self.ctx = zmq.asyncio.Context.instance()
		self.rpc_bind, self.xsub_bind, self.xpub_bind = rpc_bind, xsub_bind, xpub_bind
		self._router: Optional[zmq.asyncio.Socket] = None
		self._xsub: Optional[zmq.asyncio.Socket] = None
		self._xpub: Optional[zmq.asyncio.Socket] = None

		self.relays: Dict[str, Dict[str, Any]] = {}   # global_name -> {rpc_endpoint: str}
		self.banks: Dict[str, Dict[str, Any]] = {}     # bank_id -> {ingest, retrieve}
	
	async def _run_state_proxy(self):
		xsub = self.ctx.socket(zmq.XSUB)
		_curve_server_setup(xsub)
		xsub.sndhwm = 4000
		xsub.rcvhwm = 4000
		xsub.bind(self.xsub_bind)

		xpub = self.ctx.socket(zmq.XPUB)
		_curve_server_setup(xpub)
		xpub.sndhwm = 4000
		xpub.rcvhwm = 4000
		# XPUB receives subscribe/unsubscribe control frames from SUB clients
		xpub.setsockopt(zmq.XPUB_VERBOSE, 1)
		xpub.bind(self.xpub_bind)

		self._xsub, self._xpub = xsub, xpub
		print(f"[broker] state proxy XSUB={self.xsub_bind} <-> XPUB={self.xpub_bind}")

		poller = zmq.asyncio.Poller()
		poller.register(xsub, zmq.POLLIN)
		poller.register(xpub, zmq.POLLIN)

		try:
			while True:
				events = dict(await poller.poll())
				# Forward data frames from relays (PUB) to clients (SUB)
				if xsub in events and events[xsub] & zmq.POLLIN:
					msg = await xsub.recv_multipart()
					await xpub.send_multipart(msg)
				# Forward subscription control frames from clients to XSUB
				if xpub in events and events[xpub] & zmq.POLLIN:
					sub_msg = await xpub.recv_multipart()
					await xsub.send_multipart(sub_msg)
		finally:
			xsub.close(0)
			xpub.close(0)

	async def _run_directory(self):
		r = self.ctx.socket(zmq.ROUTER); _curve_server_setup(r)
		r.sndhwm = 1000; r.rcvhwm = 1000; r.bind(self.rpc_bind)
		self._router = r
		print(f"[broker] directory RPC at {self.rpc_bind}")

		while True:
			ident, payload = await r.recv_multipart()
			msg = loads(payload); t = msg.get("type")

			if t == "hello":
				role = msg.get("role")
				if role == "relay":
					gname, ep = msg.get("global_name"), msg.get("rpc_endpoint")
					if not gname or not ep:
						await r.send_multipart([ident, dumps({"type":"error","error":{"code":400,"message":"missing global_name/rpc_endpoint"}})]); continue
					self.relays[gname] = {"rpc_endpoint": ep}
					await r.send_multipart([ident, dumps({"type":"hello","ok":True,"role":"relay"})])
					print(f"[broker] relay up: {gname} -> {ep}")
				elif role == "bank":
					bank_id = msg.get("bank_id") or "bank"
					ingest, retrieve = msg.get("ingest"), msg.get("retrieve")
					self.banks[bank_id] = {"ingest": ingest, "retrieve": retrieve}
					await r.send_multipart([ident, dumps({"type":"hello","ok":True,"role":"bank","bank_id":bank_id})])
					print(f"[broker] bank up: {bank_id} ingest={ingest} retrieve={retrieve}")
				else:  # client
					await r.send_multipart([ident, dumps({"type":"hello","ok":True,"role":"client"})])
					print("[broker] client hello")
				continue

			if t == "rpc":
				method = msg.get("method"); rid = msg.get("id")
				if method == "list_global_names":
					result = [{"global_name": s, **info} for s, info in sorted(self.relays.items())]
					await r.send_multipart([ident, dumps({"type":"rpc_result","id":rid,"result":result})])
				elif method == "list_banks":
					result = [{"bank_id": b, **info} for b, info in sorted(self.banks.items())]
					await r.send_multipart([ident, dumps({"type":"rpc_result","id":rid,"result":result})])
				elif method == "ping":
					await r.send_multipart([ident, dumps({"type":"rpc_result","id":rid,"result":{"ok":True}})])
				else:
					await r.send_multipart([ident, dumps({"type":"rpc_error","id":rid,"error":{"code":400,"message":f"unknown method {method}"}})])
				continue

	async def serve(self):
		await asyncio.gather(self._run_state_proxy(), self._run_directory())

async def _main():
	b = DirectoryBroker()
	await b.serve()

if __name__ == "__main__":
	import asyncio
	asyncio.run(_main())
