from __future__ import annotations

import asyncio, os, uuid, time, pathlib, hashlib
from typing import Dict, Any, Optional, Tuple

import zmq, zmq.asyncio

from .util import dumps, loads
from .util import ensure_windows_selector_loop
ensure_windows_selector_loop()

BROKER_RPC = os.environ.get("LMH_RPC_CONNECT", "tcp://127.0.0.1:5750")
BROKER_XSUB = os.environ.get("LMH_XSUB_CONNECT", "tcp://127.0.0.1:5751")

DEFAULT_INGEST_BIND = os.environ.get("LMH_BANK_INGEST_BIND", "tcp://*:5761")
DEFAULT_RETRIEVE_BIND = os.environ.get("LMH_BANK_RETRIEVE_BIND", "tcp://*:5762")
DATA_DIR = os.environ.get("LMH_BANK_DATA_DIR", "./bank_data")
BANK_ID = os.environ.get("LMH_BANK_ID", "bank-1")
CHUNK_SIZE = 1_000_000  # 1 MB
HEARTBEAT_SEC = int(os.environ.get("LMH_HEARTBEAT_SECONDS", "5"))

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

class DataBank:
	"""Accepts dataset uploads and serves downloads (with checksum verification).

	Upload protocol (driver -> bank):
	  - ingest_start: {dataset_id, global_name, meta, size, sha256}
	  - ingest_chunk: {dataset_id, seq, eof:false} + [binary chunk]  -> bank replies {ingest_ack_chunk, next_seq}
	  - ingest_chunk eof:true (no chunk) -> bank verifies (size+sha256), announces dataset, replies ingest_done

	Download protocol (client -> bank):
	  - get: {dataset_id} -> meta + chunk stream
	"""
	def __init__(self, *, ingest_bind: str = DEFAULT_INGEST_BIND, retrieve_bind: str = DEFAULT_RETRIEVE_BIND, data_dir: str = DATA_DIR,
				 broker_rpc: str = BROKER_RPC, broker_xsub: str = BROKER_XSUB, bank_id: str = BANK_ID):
		self.ctx = zmq.asyncio.Context.instance()
		self.ingest_bind = ingest_bind
		self.retrieve_bind = retrieve_bind
		self.data_dir = pathlib.Path(data_dir); self.data_dir.mkdir(parents=True, exist_ok=True)
		self.broker_rpc = broker_rpc
		self.broker_xsub = broker_xsub
		self.bank_id = bank_id

		self.ingest_router: Optional[zmq.asyncio.Socket] = None
		self.retrieve_router: Optional[zmq.asyncio.Socket] = None
		self.pub: Optional[zmq.asyncio.Socket] = None
		self.dir_req: Optional[zmq.asyncio.Socket] = None  # to register/heartbeat with broker

		# simple index
		self.index_path = self.data_dir / "index.json"
		self.index: Dict[str, Dict[str, Any]] = {}
		if self.index_path.exists():
			try:
				self.index = loads(self.index_path.read_bytes())
			except Exception:
				self.index = {}

	async def _register_with_broker(self):
		req = self.ctx.socket(zmq.DEALER); _curve_client_setup(req); req.connect(self.broker_rpc)
		self.dir_req = req
		await req.send(dumps({"type":"hello","role":"bank","bank_id": self.bank_id,
							  "ingest": self.ingest_bind.replace("*","127.0.0.1"),
							  "retrieve": self.retrieve_bind.replace("*","127.0.0.1")}))
		_ = await req.recv()

		pub = self.ctx.socket(zmq.PUB); _curve_client_setup(pub); pub.connect(self.broker_xsub)
		self.pub = pub
		asyncio.create_task(self._heartbeat())

	async def _heartbeat(self):
		assert self.dir_req is not None
		while True:
			await asyncio.sleep(HEARTBEAT_SEC)
			try:
				await self.dir_req.send(dumps({"type":"heartbeat","role":"bank","bank_id": self.bank_id}))
			except Exception:
				pass

	async def _run_ingest(self):
		r = self.ctx.socket(zmq.ROUTER); _curve_server_setup(r); r.bind(self.ingest_bind)
		self.ingest_router = r
		print(f"[bank] ingest at {self.ingest_bind}")

		class Session:
			__slots__ = ("ds","gname","meta","f","hasher","next_seq","expected_size","expected_sha")
			def __init__(self, ds, gname, meta, f, expected_size, expected_sha):
				self.ds = ds; self.gname = gname; self.meta = meta; self.f = f
				self.hasher = hashlib.sha256(); self.next_seq = 0
				self.expected_size = expected_size; self.expected_sha = expected_sha

		inflight: Dict[bytes, Session] = {}

		while True:
			frames = await r.recv_multipart()
			ident, payload = frames[0], frames[1]
			hdr = loads(payload); t = hdr.get("type")

			if t == "ingest_start":
				ds = hdr.get("dataset_id") or uuid.uuid4().hex
				gname = hdr.get("global_name") or "unknown"
				meta = hdr.get("meta") or {}
				expected_size = int(hdr.get("size") or 0)
				expected_sha = hdr.get("sha256") or ""
				path = self.data_dir / f"{ds}.bin"
				f = open(path, "wb")
				inflight[ident] = Session(ds, gname, meta, f, expected_size, expected_sha)
				await r.send_multipart([ident, dumps({"type":"ingest_ack","dataset_id": ds})])
				continue

			if t == "ingest_chunk":
				sess = inflight.get(ident)
				if not sess:
					await r.send_multipart([ident, dumps({"type":"error","error":{"code":409,"message":"no ingest_start"}})])
					continue
				seq = int(hdr.get("seq") or 0)
				eof = bool(hdr.get("eof"))
				chunk = frames[2] if len(frames) > 2 else b""

				if not eof:
					if seq != sess.next_seq:
						await r.send_multipart([ident, dumps({"type":"ingest_ack_chunk","dataset_id": sess.ds, "next_seq": sess.next_seq, "status":"out_of_order"})])
						continue
					if chunk:
						sess.f.write(chunk); sess.hasher.update(chunk); sess.next_seq += 1
					await r.send_multipart([ident, dumps({"type":"ingest_ack_chunk","dataset_id": sess.ds, "next_seq": sess.next_seq})])
					continue
				else:
					sess.f.flush(); sess.f.close()
					path = self.data_dir / f"{sess.ds}.bin"
					size = path.stat().st_size
					sha = sess.hasher.hexdigest()
					ok = True
					err = None
					if sess.expected_size and size != sess.expected_size:
						ok = False; err = f"size mismatch: got {size}, expected {sess.expected_size}"
					if sess.expected_sha and sha != sess.expected_sha:
						ok = False; err = f"sha mismatch: got {sha}, expected {sess.expected_sha}"
					if ok:
						self.index[sess.ds] = {"path": str(path), "size": size, "sha256": sha, "ts": time.time(), "meta": sess.meta, "global_name": sess.gname}
						self.index_path.write_bytes(dumps(self.index))
						assert self.pub is not None
						topic = f"dataset.{self.bank_id}".encode("utf-8")
						await self.pub.send_multipart([topic, dumps({"dataset_id": sess.ds, "bank_id": self.bank_id, "size": size, "sha256": sha, "global_name": sess.gname})])
						await r.send_multipart([ident, dumps({"type":"ingest_done","dataset_id": sess.ds, "size": size, "sha256": sha})])
					else:
						await r.send_multipart([ident, dumps({"type":"error","dataset_id": sess.ds, "error":{"code":422,"message":err}})])
					inflight.pop(ident, None)
					continue

	async def _run_retrieve(self):
		r = self.ctx.socket(zmq.ROUTER); _curve_server_setup(r); r.bind(self.retrieve_bind)
		self.retrieve_router = r
		print(f"[bank] retrieve at {self.retrieve_bind}")
		while True:
			ident, payload = await r.recv_multipart()
			hdr = loads(payload)
			if hdr.get("type") != "get":
				await r.send_multipart([ident, dumps({"type":"error","error":{"code":400,"message":"expected get"}})]); continue
			ds = hdr.get("dataset_id")
			info = self.index.get(ds)
			if not info:
				await r.send_multipart([ident, dumps({"type":"error","error":{"code":404,"message":"not found"}})]); continue
			path = info["path"]; size = info["size"]; sha = info["sha256"]; meta = info.get("meta", {})
			await r.send_multipart([ident, dumps({"type":"meta","dataset_id": ds, "size": size, "sha256": sha, "meta": meta})])
			with open(path, "rb") as f:
				seq = 0
				while True:
					chunk = f.read(CHUNK_SIZE)
					if not chunk:
						break
					await r.send_multipart([ident, dumps({"type":"chunk","dataset_id": ds, "seq": seq, "eof": False}), chunk])
					seq += 1
				await r.send_multipart([ident, dumps({"type":"chunk","dataset_id": ds, "seq": seq, "eof": True})])

	async def serve(self):
		await self._register_with_broker()
		await asyncio.gather(self._run_ingest(), self._run_retrieve())

async def _main():
	bank = DataBank()
	await bank.serve()

if __name__ == "__main__":
	import asyncio
	asyncio.run(_main())
