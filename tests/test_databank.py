from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from labmesh import DataBank
from labmesh.relay import upload_dataset
from labmesh.util import dumps, loads


class TestUploadDownloadRoundtrip:
    async def test_small_payload_roundtrip(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        client = await mesh.start_client(broker)

        payload = b"hello labmesh " * 1000
        dataset_id = await upload_dataset(f"tcp://127.0.0.1:{bank.ingest_port}", payload, relay_id="r1", meta={"note": "test"})

        bc = await client.get_databank_agent("bank-0")
        dest = tmp_path / "downloaded.bin"
        meta = await bc.download(dataset_id, str(dest))

        assert dest.read_bytes() == payload
        assert meta["size"] == len(payload)
        assert meta["sha256"] == hashlib.sha256(payload).hexdigest()

    async def test_multi_chunk_upload_exceeding_chunk_size(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)

        payload = os.urandom(2_500_000)  # > CHUNK_SIZE (1 MB) - forces several chunks
        dataset_id = await upload_dataset(f"tcp://127.0.0.1:{bank.ingest_port}", payload, relay_id="r1")

        assert (tmp_path / f"{dataset_id}.bin").read_bytes() == payload
        assert bank.index[dataset_id]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert bank.index[dataset_id]["size"] == len(payload)

    async def test_dataset_announced_to_subscribers(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        client = await mesh.start_client(broker)

        events = []
        client.on_dataset(lambda info: events.append(info))

        dataset_id = await upload_dataset(f"tcp://127.0.0.1:{bank.ingest_port}", b"x" * 100, relay_id="r1")

        for _ in range(60):
            if events:
                break
            await asyncio.sleep(0.05)

        assert events
        assert events[0]["dataset_id"] == dataset_id
        assert events[0]["bank_id"] == "bank-0"

    async def test_get_missing_dataset_raises(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        await mesh.start_databank("bank-0", tmp_path, broker)
        client = await mesh.start_client(broker)
        bc = await client.get_databank_agent("bank-0")
        with pytest.raises(RuntimeError):
            await bc.download("does-not-exist", str(tmp_path / "out.bin"))

    async def test_index_persists_across_restart(self, mesh, tmp_path):
        broker = await mesh.start_broker()
        bank1 = await mesh.start_databank("bank-0", tmp_path, broker)
        dataset_id = await upload_dataset(f"tcp://127.0.0.1:{bank1.ingest_port}", b"persisted data", relay_id="r1")
        assert dataset_id in bank1.index

        # a fresh DataBank instance pointed at the same data_dir, without
        # serving - only its __init__-time index load is under test
        bank2 = DataBank(
            ingest_bind="tcp://*:0", retrieve_bind="tcp://*:0",
            data_dir=str(tmp_path), broker_rpc="tcp://BROKER:0", broker_xsub="tcp://BROKER:0",
            bank_id="bank-0",
        )
        assert dataset_id in bank2.index
        assert bank2.index[dataset_id]["sha256"] == bank1.index[dataset_id]["sha256"]


class TestIngestProtocolEdgeCases:
    async def test_chunk_before_any_start_is_rejected(self, mesh, tmp_path, dealer_to):
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        sock = dealer_to(f"tcp://127.0.0.1:{bank.ingest_port}")

        await sock.send_multipart([dumps({"type": "ingest_chunk", "dataset_id": "never-started", "seq": 0, "eof": False}), b"data"])
        reply = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))
        assert reply["type"] == "error"
        assert reply["error"]["code"] == 409

    async def test_out_of_order_chunk_reports_expected_seq(self, mesh, tmp_path, dealer_to):
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        sock = dealer_to(f"tcp://127.0.0.1:{bank.ingest_port}")

        await sock.send(dumps({"type": "ingest_start", "dataset_id": "ooo-1", "relay_id": "r1", "meta": {}}))
        _ = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))  # ingest_ack

        await sock.send_multipart([dumps({"type": "ingest_chunk", "dataset_id": "ooo-1", "seq": 5, "eof": False}), b"data"])
        reply = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))

        assert reply["status"] == "out_of_order"
        assert reply["next_seq"] == 0

    async def test_declared_size_mismatch_is_rejected(self, mesh, tmp_path, dealer_to):
        """The databank's own verification logic (databank.py) does reject
        a stream that doesn't match its declared size/sha256 - but see
        test_upload_dataset_helper_never_declares_expected_size_or_hash
        below for the catch: nothing in this codebase's own upload path
        ever populates those fields, so this check is currently dead code
        in normal use."""
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)
        sock = dealer_to(f"tcp://127.0.0.1:{bank.ingest_port}")

        await sock.send(dumps({"type": "ingest_start", "dataset_id": "bad-1", "relay_id": "r1",
                                "meta": {}, "size": 9999, "sha256": "0" * 64}))
        _ = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))

        await sock.send_multipart([dumps({"type": "ingest_chunk", "dataset_id": "bad-1", "seq": 0, "eof": False}), b"short payload"])
        _ = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))

        await sock.send_multipart([dumps({"type": "ingest_chunk", "dataset_id": "bad-1", "seq": 1, "eof": True})])
        reply = loads(await asyncio.wait_for(sock.recv(), timeout=2.0))

        assert reply["type"] == "error"
        assert reply["error"]["code"] == 422
        assert "bad-1" not in bank.index

    async def test_upload_dataset_helper_never_declares_expected_size_or_hash(self, mesh, tmp_path):
        """labmesh.relay.upload_dataset() sends ingest_start with only
        dataset_id/relay_id/meta - no `size` or `sha256` - so
        DataBank._run_ingest's expected_size/expected_sha are always 0/"",
        and the `if sess.expected_size and ...` / `if sess.expected_sha and
        ...` checks in the eof branch are always skipped. A transfer that
        silently dropped or corrupted bytes in flight would still be
        reported as ingest_done with no error, through this call path."""
        broker = await mesh.start_broker()
        bank = await mesh.start_databank("bank-0", tmp_path, broker)

        payload = b"whatever bytes actually arrive are trusted as-is"
        dataset_id = await upload_dataset(f"tcp://127.0.0.1:{bank.ingest_port}", payload, relay_id="r1")

        # it "succeeded" - there was never an expectation to fail to meet
        assert dataset_id in bank.index
        assert bank.index[dataset_id]["size"] == len(payload)
