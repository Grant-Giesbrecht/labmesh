# labmesh
Mesh networking package using ZeroMQ specifically designed for constellation.

## How It Works

labmesh is a ZeroMQ-based mesh networking package designed for the lab instrument automation package `constellation-core`.

labmesh defines four types of nodes:
 - `broker`: Each network must have exactly one broker. The broker tracks what nodes are connected to the network, and initializes new nodes by passing address info around.
 - `client`: Client nodes send commands to relay nodes, and receive updates from the PUB/SUB model.
 - `relay`: Relay nodes are the 'worker' nodes that connect to lab instruments and relay commands from one or more clients to one or more physical instruments.
 - `databank`: Databank nodes allow data to be stored and managed by a specific network node. Relays publish data to banks, and clients can query the banks for data results.

## Basic Example

A basic example labmesh program will need to define a node of each of the four types. Network configuration info is provided to the node programs via a `toml` file. In the included basic example, that is the `labmesh.toml` file.

### Example Config File

``` TOML
# Configuration file for application ports and addresses

# ── Broker ────────────────────────────────────────────────────────────────────
[broker]
rpc_bind  = "tcp://*:5750"
xsub_bind = "tcp://*:5751"   # relays'/banks' PUB connect here
xpub_bind = "tcp://*:5752"   # clients' SUB connect here
ttl_seconds = 30
address = "127.0.0.1"

# ── Relay ─────────────────────────────────────────────────────────────────────
[relay]
default_rpc_bind = "tcp://*:5850"      # Relay RPC server - each relay needs a different port!
#state_interval = 1.0
broker_rpc  = "tcp://BROKER:5750"
broker_xsub = "tcp://BROKER:5751"
heartbeat_seconds = 5
upload_timeout  = 10
upload_retries  = 5
upload_chunk    = 1000000
default_address = "127.0.0.1"

# ── Data bank ─────────────────────────────────────────────────────────────────
[bank]
ingest_bind   = "tcp://*:5761"  # drivers upload here
retrieve_bind = "tcp://*:5762"  # clients download here
default_data_dir = "./bank_data" # Makes most sense for each bank to explicitly set
broker_rpc  = "tcp://BROKER:5750"
broker_xsub = "tcp://BROKER:5751"
heartbeat_seconds = 5
default_address = "127.0.0.1"

# ── Client ────────────────────────────────────────────────────────────────────
[client]
broker_rpc = "tcp://BROKER:5750"
broker_xpub = "tcp://BROKER:5752"
default_address = "127.0.0.1"
```