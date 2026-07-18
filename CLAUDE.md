# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

labmesh is a ZeroMQ-based mesh networking package for the lab-instrument-automation package `constellation-core`. It is a small, dependency-light Python library (`pyzmq` is the only runtime dependency) built entirely on `asyncio` + `zmq.asyncio`. There is no test suite, linter config, or CI in this repo yet.

## Commands

```bash
# Install in editable mode (only runtime dep is pyzmq)
pip install -e .

# Generate a CURVE keypair for encrypting a node's sockets (installed console script)
lm-keygen --format env   # or: json | toml

# Run the example 4-node network (each in its own terminal, from examples/)
python run_broker.py --toml labmesh.toml
python run_bank.py --toml labmesh.toml --bank_id bank-0
python run_driver_psu.py --toml labmesh.toml --relay_id Inst-0
python run_client.py --toml labmesh.toml
```

There are no automated tests, build step, or lint config currently configured — verify changes by running the example network above.

## Architecture

labmesh defines a mesh with exactly one **broker** and any number of **relay**, **databank**, and **client** nodes, all communicating over raw ZeroMQ sockets with hand-rolled JSON-over-multipart-frame protocols (see `labmesh/util.py:dumps`/`loads`). Each node type lives in its own module and is largely self-contained:

- `labmesh/broker.py` — `DirectoryBroker`. Runs two independent coroutines via `asyncio.gather`:
  - `_run_directory`: a ROUTER socket handling request/reply `hello`/`rpc` messages (relay/bank registration, `list_relay_ids`, `list_banks`, `ping`). This is the only place node presence is tracked (in-memory dicts `self.relays` / `self.banks`; no persistence, no TTL expiry despite `ttl_seconds` in the example config).
  - `_run_state_proxy`: a raw XSUB/XPUB forwarder that relays PUB traffic from relays/banks straight through to SUB clients. The broker never inspects this traffic — it's pure pub/sub fan-out, keyed by topic prefix (`state.<relay_id>`, `dataset.<bank_id>`).

- `labmesh/relay.py` — `RelayAgent` wraps a user-supplied "driver" object (any Python object, e.g. an instrument driver like the `MockPSU` in the README) and exposes it to the network. Three coroutines run concurrently via `run()`:
  - `_register`: DEALER socket sends a one-shot `hello` to the broker's RPC port.
  - `_serve_rpc`: ROUTER socket that dispatches incoming RPC calls directly onto the wrapped driver object via `getattr(self.relay, method)` — i.e. **any public method on the driver object is remotely callable by method name**, with dict params passed as `**kwargs`, list params as positional args.
  - `_serve_state`: PUB socket that calls `self.relay.poll()` (if present) on a timer and publishes the result to the broker's XSUB under topic `state.<relay_id>`.
  - `upload_dataset()` is a standalone coroutine (not a method) used by relay-side code to stream a dataset to a databank's ingest socket in chunks (see the ingest protocol below).

- `labmesh/databank.py` — `DataBank` stores uploaded datasets on disk and serves them back out. Two coroutines via `serve()`:
  - `_run_ingest`: ROUTER socket implementing a small stateful chunked-upload protocol keyed by sender identity (`inflight: Dict[ident, Session]`): `ingest_start` → `ingest_chunk` (1..N, in-order, each acked) → final `ingest_chunk` with `eof:true`, at which point size/sha256 are verified against what was declared in `ingest_start`, the file is recorded in a JSON index (`<data_dir>/index.json`), and a `dataset.<bank_id>` event is published to the broker's XSUB.
  - `_run_retrieve`: ROUTER socket implementing the download side (`get` → `meta` + chunked `chunk` frames terminated by `eof:true`), reading the index built by ingest.
  - Also registers with the broker and sends a periodic heartbeat (currently a no-op on the broker side — see TODOs in source).

- `labmesh/client.py` — the consumer-facing API, built from three classes:
  - `DirectorClientAgent`: connects to the broker's RPC + XPUB, auto-subscribes to `state.` and `dataset.` topics, and dispatches to user-registered callbacks (`on_state`, `on_dataset`). `get_relay_agent(relay_id)` / `get_databank_agent(bank_id)` look the target up via the broker directory RPC and return a connected client.
  - `RelayClient`: a DEALER-socket RPC stub for one relay. Uses `__getattr__` so **any attribute access becomes an async RPC call by that name** (see `PSUClient` subclass pattern in the README, which instead defines explicit typed wrapper methods calling `super().call(...)`).
  - `BankClient`: DEALER-socket client for the databank retrieve protocol (mirrors `_run_retrieve` above), writes downloaded datasets to a local file path and verifies the reported checksum size.

- `labmesh/security.py` — `lm-keygen` CLI entry point; generates a ZMQ CURVE keypair. All sockets in every module call a local `_curve_server_setup`/`_curve_client_setup` helper that reads `ZMQ_SERVER_SECRETKEY`/`ZMQ_SERVER_PUBLICKEY`/`ZMQ_CLIENT_SECRETKEY`/`ZMQ_CLIENT_PUBLICKEY` from the environment and enables CURVE only if the relevant vars are set — encryption is opt-in and per-process via env vars, not config-driven.

- `labmesh/util.py` — shared helpers: `read_toml_config` (uses stdlib `tomllib` on 3.11+, falls back to `tomli` — note `tomli` is *not* currently listed in `pyproject.toml` dependencies, so installs on Python 3.10 need it added manually), `dumps`/`loads` (compact JSON \<-\> bytes), `ensure_windows_selector_loop` (forces the Windows selector event loop policy, called at import time by every networking module since ZMQ's asyncio integration needs it on Windows), `env_int`.

### Conventions to know before editing

- **Address placeholders**: config values use two substitution conventions that call sites must replicate manually (no central templating): `"tcp://*:PORT"` → bind address, with `*` replaced by a real host when advertised to peers (`local_address`); `"tcp://BROKER:PORT"` → connect address, with `BROKER` replaced by `broker_address`. Every node constructor takes these as separate `local_address`/`broker_address` string args and does the `.replace()` itself.
- **Node config comes from TOML**, not hardcoded env vars. `examples/labmesh.toml` (loopback) and `examples/labmesh_remote.toml` (LAN, multiple hosts) show the `[broker]`/`[relay]`/`[bank]`/`[client]` table shape; `read_toml_config` + manual dict indexing is the pattern used in every `examples/run_*.py` script and should be followed for new node scripts.
- **RPC envelope shape** is consistent everywhere: `{"type": "rpc", "rpc_uuid": <hex>, "method": <str>, "params": <dict|list>}` in, `{"type": "rpc_result", "rpc_uuid": ..., "result": ...}` or `{"type": "rpc_error", "rpc_uuid": ..., "error": {"code", "message"}}` out. Callers loop on `recv()` discarding replies whose `rpc_uuid` doesn't match (sockets are shared/multiplexed, not one-request-per-socket).
- **New relay-exposed methods**: since `_serve_rpc` dispatches by `getattr(driver, method)`, adding a capability to a driver object is enough to expose it over RPC — no registration step needed. Keep driver methods synchronous and JSON-serializable in/out (they're called directly inside the async RPC loop, not offloaded to a thread).
- Sockets are never explicitly closed on the happy path; processes are expected to run until killed (`asyncio.run(...serve())` in every example entry point).
