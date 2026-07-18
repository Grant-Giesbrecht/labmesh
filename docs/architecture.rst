How labmesh works
===================

This page goes under the hood: what connects to what, what's on the wire,
and — in detail — where CURVE keys come from, what they actually encrypt,
and how the shared network password moves around and gets checked. If
you just want to run a network, see :doc:`usage` instead.

Topology
---------

A labmesh network has exactly one broker. Every other node connects out
*to* the broker to register and to look things up, but relays and
databanks also run their own servers that clients (and, for uploads,
relays) connect to directly:

.. mermaid::

   flowchart LR
       subgraph Broker
           direction TB
           BR["ROUTER :5750\ndirectory"]
           BX["XSUB :5751"]
           BP["XPUB :5752"]
           BX --> BP
       end

       Relay["Relay\nROUTER :5850+ (RPC)"]
       Bank["Databank\nROUTER (ingest)\nROUTER (retrieve)"]
       Client["Client"]

       Relay -- "hello / heartbeat" --> BR
       Bank  -- "hello / heartbeat" --> BR
       Client -- "hello / rpc" --> BR

       Relay -- "PUB state.<relay_id>" --> BX
       Bank  -- "PUB dataset.<bank_id>" --> BX
       BP -- "SUB state.* / dataset.*" --> Client

       Client -- "rpc (direct)" --> Relay
       Relay  -- "ingest_start / ingest_chunk" --> Bank
       Client -- "get" --> Bank

Three things to notice:

- The broker's RPC socket is a **directory**, not a proxy. A client asks
  it *"where is relay Inst-0?"* once (:meth:`~labmesh.client.DirectorClientAgent.get_relay_agent`),
  gets back an address, and from then on talks to that relay's own ROUTER
  socket directly — the broker is never in the path of an RPC call.
- The XSUB/XPUB pair *is* a proxy: the broker forwards those frames
  byte-for-byte without ever parsing them (:func:`labmesh.broker.DirectoryBroker._run_state_proxy`,
  ``broker.py:51``). That matters later, in :ref:`what-isnt-covered`.
- Databanks are reached two ways: relays push data in on the *ingest*
  socket, clients pull it back out on the separate *retrieve* socket.

Message envelope
------------------

There's no schema registry or IDL — every message is a compact JSON
object (:func:`labmesh.util.dumps` / :func:`labmesh.util.loads`) inside a
ZeroMQ multipart frame. Two shapes cover almost everything:

.. code-block:: text

   → {"type": "rpc", "rpc_uuid": "<hex>", "method": "<name>", "params": {...}}
   ← {"type": "rpc_result", "rpc_uuid": "<hex>", "result": ...}
   ← {"type": "rpc_error",  "rpc_uuid": "<hex>", "error": {"code": ..., "message": ...}}

Callers loop on ``recv()`` and discard replies whose ``rpc_uuid`` doesn't
match, because DEALER sockets are shared and multiplexed rather than
one-request-per-connection — a single :class:`~labmesh.client.RelayClient`
can have several calls in flight at once.

A relay's RPC dispatch is intentionally simple: ``method`` is looked up
with ``getattr(driver, method)`` and called with ``params`` as keyword
arguments (dict) or positional arguments (list) — see
:meth:`labmesh.relay.RelayAgent._serve_rpc`, ``relay.py:74``. Adding a
capability to a driver object is enough to expose it; there's no
registration step.

Two request/reply flows
-------------------------

**Registering and calling a relay.** A relay's ``hello`` tells the broker
its address; the broker never brokers the call itself:

.. mermaid::

   sequenceDiagram
       participant R as Relay
       participant Br as Broker
       participant C as Client

       R->>Br: hello {role: relay, relay_id, rpc_endpoint}
       Br-->>R: hello {ok: true}
       Note over Br: relay_id → rpc_endpoint stored in self.relays (in-memory, no persistence)

       C->>Br: hello {role: client}
       Br-->>C: hello {ok: true}
       C->>Br: rpc list_relay_ids
       Br-->>C: rpc_result [{relay_id, rpc_endpoint}, ...]

       Note over C,R: client now has the relay's address — broker drops out
       C->>R: rpc set_voltage {value: 2.5}
       R-->>C: rpc_result {ok: true, voltage: 2.5}

**Uploading and downloading a dataset.** The relay pushes to the databank
in fixed-size chunks with a running SHA-256; the databank verifies size
and hash before it announces anything:

.. mermaid::

   sequenceDiagram
       participant R as Relay
       participant D as Databank
       participant Br as Broker
       participant C as Client

       R->>D: ingest_start {dataset_id, size, sha256}
       D-->>R: ingest_ack
       loop each chunk
           R->>D: ingest_chunk {seq, eof:false} + bytes
           D-->>R: ingest_ack_chunk {next_seq}
       end
       R->>D: ingest_chunk {eof:true}
       D->>D: verify size + sha256, write index.json
       D-->>R: ingest_done
       D-)Br: PUB dataset.<bank_id> {dataset_id, size, sha256}
       Br-)C: XPUB forwards it to subscribers
       C->>D: get {dataset_id}
       D-->>C: meta {size, sha256}
       loop each chunk
           D-->>C: chunk {seq, eof:false} + bytes
       end
       D-->>C: chunk {eof:true}

.. _curve-encryption:

CURVE encryption: where keys come from, what they cover
----------------------------------------------------------

Every server-style socket in labmesh (every ``ROUTER``, the ``XSUB``, the
``XPUB``) can run `ZMQ CURVE <https://rfc.zeromq.org/spec/26/>`_, and
every client-style socket (every ``DEALER``, the ``SUB``) can connect to
one. It's entirely opt-in, controlled purely by which environment
variables are set in each *process's* environment — nothing in a TOML
file turns it on.

**Generating keys.** ``lm-keygen`` (``labmesh/security.py``, installed as
a console script from ``pyproject.toml``) wraps :func:`zmq.curve_keypair`
and prints a public/secret pair in one of three formats:

.. code-block:: bash

   lm-keygen --format env    # ZMQ_PUBLICKEY="..." / ZMQ_SECRETKEY="..."
   lm-keygen --format json   # {"public": "...", "secret": "..."}
   lm-keygen --format toml   # [curve.server]\npublic = "..."\nsecret = "..."

It always prints the *generic* names ``ZMQ_PUBLICKEY`` / ``ZMQ_SECRETKEY``
— it has no concept of "this run is for the server pair" vs. "this run is
for a client pair". You rename them yourself when exporting, into
whichever of the four variables below the process actually needs.

**One shared server keypair, any number of client keypairs.** This is the
part that's easy to get wrong by guessing: every ``_curve_server_setup``
helper (one per module — ``broker.py:18``, ``relay.py:19``,
``databank.py:22``) reads the *same* two variable names,
``ZMQ_SERVER_SECRETKEY`` / ``ZMQ_SERVER_PUBLICKEY``. And every
``_curve_client_setup`` helper trusts the peer it connects to by reading
``ZMQ_SERVER_PUBLICKEY`` as ``curve_serverkey`` — the *same* variable
name again. That means the broker's directory socket, every relay's RPC
socket, and every databank's ingest/retrieve sockets must all present the
**identical** CURVE server keypair, because any client anywhere in the
mesh checks incoming servers against that one public key. Give one relay
a different server keypair from the broker's and every client that tries
to reach it will simply hang at the handshake.

The client keypair (``ZMQ_CLIENT_SECRETKEY`` / ``ZMQ_CLIENT_PUBLICKEY``)
has no such constraint. Nothing on the server side checks *which* client
key connected — see `what CURVE does not do <no-allowlist_>`_ below — so it's fine to reuse one
client keypair everywhere, or generate a fresh one per node; both are
equally (un)restrictive.

.. mermaid::

   flowchart LR
       KG(["lm-keygen"]) --> SRV["Server keypair\n(secret + public)"]
       KG --> CLI["Client keypair\n(secret + public)"]

       SRV ==>|"identical pair, every server socket"| Servers["Broker ROUTER/XSUB/XPUB\nRelay RPC ROUTER\nDatabank ingest/retrieve ROUTERs"]
       SRV -.->|"public half only, as curve_serverkey"| Clients["Relay → Broker (DEALER, PUB)\nDatabank → Broker (DEALER, PUB)\nClient → Broker/Relay/Databank (DEALER, SUB)"]
       CLI ==> Clients

Concretely, each role needs:

.. list-table::
   :widths: 18 41 41
   :header-rows: 1

   * - Node
     - Needs (as a CURVE **server**)
     - Needs (as a CURVE **client**)
   * - Broker
     - ``ZMQ_SERVER_SECRETKEY``, ``ZMQ_SERVER_PUBLICKEY``
     - *(never connects out)*
   * - Relay
     - ``ZMQ_SERVER_SECRETKEY``, ``ZMQ_SERVER_PUBLICKEY`` — for its own RPC ROUTER
     - ``ZMQ_CLIENT_SECRETKEY``, ``ZMQ_CLIENT_PUBLICKEY``, ``ZMQ_SERVER_PUBLICKEY`` — to reach the broker
   * - Databank
     - ``ZMQ_SERVER_SECRETKEY``, ``ZMQ_SERVER_PUBLICKEY`` — for ingest + retrieve ROUTERs
     - ``ZMQ_CLIENT_SECRETKEY``, ``ZMQ_CLIENT_PUBLICKEY``, ``ZMQ_SERVER_PUBLICKEY`` — to reach the broker
   * - Client
     - *(never binds a socket)*
     - ``ZMQ_CLIENT_SECRETKEY``, ``ZMQ_CLIENT_PUBLICKEY``, ``ZMQ_SERVER_PUBLICKEY`` — to reach the broker, a relay, and a databank

**What's actually encrypted, and when.** Each socket decides once, at
bind/connect time, whether CURVE is on — the check is a plain
``if sec and pub:`` in every ``_curve_*_setup`` helper. If it's on, *every*
frame on that socket is encrypted for that socket's lifetime, including
the ``hello``/``rpc`` JSON envelopes and any raw dataset bytes flowing
through ingest/retrieve. There's no per-message opt-out and no partial
encryption. The one gotcha: CURVE is symmetric by construction, so if one
side of a connection has it configured and the other doesn't, the
handshake never completes — the connection just sits there. That failure
mode looks identical to "the other node isn't running yet," which is
worth knowing before you spend twenty minutes checking ports.

.. _no-allowlist:

**What CURVE does *not* do.** Every ``_curve_server_setup`` sets
``sock.curve_server = True`` and stops there — no ZAP handler is
registered anywhere in the codebase. Per `libzmq's default
<https://rfc.zeromq.org/spec/27/>`_, a CURVE server with no ZAP handler
runs in ``CURVE_ALLOW_ANY`` mode: it accepts *any* client that completes
the CURVE handshake with a well-formed keypair, not only a keypair it was
told to expect. CURVE here buys transport confidentiality and integrity —
nobody without a matching server public key can even open the connection,
and nobody sniffing the wire can read it — but it is not an allowlist of
approved clients. That gap is what the network password (below) closes,
at the application layer instead of the transport layer.

The shared network password
-----------------------------

Added on top of CURVE, independent of whether CURVE is even configured:
every ``hello``, ``rpc``, ``ingest_start``, and ``get`` message carries a
``password`` field, checked against one shared secret before the broker,
relay, or databank acts on it.

**Where it lives.** :func:`labmesh.util.network_password` (``util.py:37``)
reads ``LMH_NETWORK_PASSWORD`` from the process environment — nothing
more. :func:`labmesh.util.check_password` (``util.py:45``) compares a
message's ``password`` field against it with :func:`hmac.compare_digest`,
so a wrong guess can't be timed byte-by-byte. If a node's own
``LMH_NETWORK_PASSWORD`` is empty (including simply unset), that node
skips the check entirely and accepts anything — auth is opt-in, exactly
like CURVE.

**How it moves.** Unlike the CURVE keypair, there is no mechanism that
copies this value around the network for you. The broker is the only
process that ever prompts for it
(:func:`labmesh.util.prompt_network_password`, called from
``examples/run_broker.py`` at boot); every other node simply needs the
*identical* string already sitting in ``LMH_NETWORK_PASSWORD`` in its own
shell before it starts. Get it to each host the same way you'd get it any
shared secret to a machine you administer — out of band, not over the
mesh.

.. mermaid::

   flowchart LR
       Op(["Operator"]) -->|types once, at broker boot| PromptB["run_broker.py\nprompt_network_password()"]
       PromptB --> EnvB(("Broker's\nLMH_NETWORK_PASSWORD"))
       Op -.->|copied by hand, no auto-sync| EnvR(("Relay's\nLMH_NETWORK_PASSWORD"))
       Op -.-> EnvD(("Databank's\nLMH_NETWORK_PASSWORD"))
       Op -.-> EnvC(("Client's\nLMH_NETWORK_PASSWORD"))

**How it's checked.** The password rides inside the same JSON envelope as
everything else — after any CURVE decryption has already happened, so if
CURVE is on, the password itself is encrypted in transit too. Each
gate checks it before doing anything else with the message:

.. mermaid::

   sequenceDiagram
       participant C as Client
       participant Br as Broker

       C->>Br: rpc {method: "list_relay_ids", password: "<LMH_NETWORK_PASSWORD>"}
       Br->>Br: check_password(msg, network_password())
       alt password matches, or broker's own password is unset
           Br-->>C: rpc_result [...]
       else password missing or wrong
           Br-->>C: rpc_error {code: 401, message: "invalid password"}
       end

.. list-table:: Where the check happens
   :widths: 18 34 24 24
   :header-rows: 1

   * - Node
     - Socket
     - Checked on
     - Rejection
   * - Broker
     - directory ROUTER (``broker.py:102``)
     - ``hello``, ``rpc``
     - ``hello`` reply with ``ok: false``, or ``rpc_error`` 401
   * - Relay
     - RPC ROUTER (``relay.py:74``)
     - ``rpc``
     - ``rpc_error`` 401
   * - Databank
     - ingest ROUTER (``databank.py:137``)
     - ``ingest_start``
     - ``error`` 401 — rejected before a file is even opened
   * - Databank
     - retrieve ROUTER (``databank.py:303``)
     - ``get``
     - ``error`` 401

.. _what-isnt-covered:

What neither mechanism covers
--------------------------------

- **The state/dataset pub-sub proxy is unauthenticated by design.** The
  broker's ``_run_state_proxy`` (``broker.py:51``) forwards raw multipart
  frames between XSUB and XPUB without ever deserializing them — there's
  no request/reply exchange to attach a ``password`` field to, and adding
  one would mean turning a zero-copy fan-out into a deep-inspecting proxy
  for every state update. If CURVE isn't configured on the XSUB/XPUB
  pair, anyone who can reach those ports can publish or subscribe to
  ``state.*``/``dataset.*`` traffic regardless of the network password.
- **One secret, not per-node identity.** The network password is a single
  shared value for the whole mesh, not a credential per relay, per
  databank, or per client. Anyone who has it can act as any node — call
  any method a relay exposes, upload or retrieve any dataset. There's
  still no per-method authorization or ACL.
- **No allowlist of CURVE clients**, as covered above — ``CURVE_ALLOW_ANY``
  applies everywhere a server socket is configured.

.. list-table:: CURVE vs. the network password, side by side
   :widths: 22 39 39
   :header-rows: 1

   * -
     - CURVE encryption
     - Network password
   * - Layer
     - Transport (the ZMQ socket itself)
     - Application (a JSON field inside the envelope)
   * - Protects
     - Confidentiality + integrity of every byte on the wire
     - Whether a *message* gets acted on
   * - Configured via
     - ``ZMQ_SERVER_*`` / ``ZMQ_CLIENT_*`` env vars
     - ``LMH_NETWORK_PASSWORD`` env var
   * - Default state
     - Off (plaintext)
     - Off (no check)
   * - Covers the pub/sub state proxy
     - Yes, if configured
     - No — can't be, by construction
   * - Distinguishes individual peers
     - No (``CURVE_ALLOW_ANY``, no ZAP handler)
     - No (one shared secret for the whole mesh)

Running both together is the closest labmesh comes to a real security
posture today: CURVE keeps the wire private, the password keeps
uninvited processes from being acted on. Neither one, alone or together,
is an identity system — that would need a ZAP handler with a real
allowlist and per-node credentials, which is future work, not present
behavior.
