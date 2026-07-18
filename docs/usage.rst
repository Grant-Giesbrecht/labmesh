Using labmesh
=============

This page has two examples. The **minimal** one is the smallest network
that actually works — one broker, one relay, one client, no config file,
no security — so you can see the whole request/reply round trip in about
thirty lines. The **fully-featured** one is the four-node network shipped
in ``examples/``: a relay wrapping a mock instrument, a databank storing
uploaded datasets, and a client subscribing to live state updates and
downloading data as it arrives, with CURVE encryption and the shared
network password both switched on.

If you haven't already, install labmesh first — see :doc:`installation`.

Minimal example
----------------

Three files, three processes, no TOML file. A relay wraps a plain
``Counter`` object; the client looks it up through the broker and calls
one of its methods.

.. code-block:: text

   examples/minimal/
   ├── broker.py
   ├── relay.py
   └── client.py

**The broker.** Binds the three ports every labmesh network needs: an RPC
port for the directory, and an XSUB/XPUB pair that fans published state
out to subscribers.

.. literalinclude:: ../examples/minimal/broker.py
   :language: python
   :linenos:

**The relay.** ``RelayAgent`` wraps *any* Python object — here, a small
counter. Every public method on that object becomes a remote procedure
call by name; if the object has a ``poll()`` method, its return value is
published as periodic state.

.. literalinclude:: ../examples/minimal/relay.py
   :language: python
   :linenos:

**The client.** Connects to the broker, looks up the relay named
``counter-0``, and calls ``increment`` on it exactly as if it were a local
method.

.. literalinclude:: ../examples/minimal/client.py
   :language: python
   :linenos:

Run each in its own terminal, from ``examples/minimal/``:

.. code-block:: bash

   python broker.py
   python relay.py
   python client.py     # prints {'n': 5}

There's no allocation step and no schema to register — ``increment`` became
callable the moment it existed on the wrapped object. That's the whole
model; the fully-featured example below just adds more pieces on top of it.

Fully-featured example
------------------------

This is the network in ``examples/``: a broker, a relay driving a mock
power supply (``MockPSU``), a databank, and a client that both issues
commands and reacts to events. Every node reads its addresses from one
shared TOML file instead of hardcoding them.

.. literalinclude:: ../examples/labmesh.toml
   :language: toml

``BROKER`` in a connect address is replaced with the broker's real host at
runtime (``broker_address`` / ``local_address`` in each node's
constructor) — see :doc:`architecture` for why addresses are written this
way instead of resolved centrally.

**The broker** — reads its three bind addresses from the config and serves:

.. literalinclude:: ../examples/run_broker.py
   :language: python

**The relay** — wraps ``MockPSU``, a stand-in instrument driver with
``set_voltage``, ``set_output``, ``get_state``, and ``poll`` methods, and
separately uploads a synthetic ~4 MB dataset to the databank every minute
using the standalone :func:`labmesh.relay.upload_dataset` helper:

.. literalinclude:: ../examples/run_driver_psu.py
   :language: python

**The databank** — stores whatever relays upload and serves it back out;
almost all of the work happens inside :class:`labmesh.databank.DataBank`
itself, so the entry point is little more than config plumbing:

.. literalinclude:: ../examples/run_bank.py
   :language: python

**The client** — subscribes to both event feeds (``on_state``,
``on_dataset``), drives the PSU directly through a typed wrapper
(``PSUClient``), and downloads each new dataset as its announcement
arrives:

.. literalinclude:: ../examples/run_client.py
   :language: python

Run each in its own terminal, from ``examples/``:

.. code-block:: bash

   python run_broker.py     --toml labmesh.toml
   python run_bank.py       --toml labmesh.toml --bank_id bank-0
   python run_driver_psu.py --toml labmesh.toml --relay_id Inst-0
   python run_client.py     --toml labmesh.toml

The client prints the relay/bank directory, sets the PSU's voltage to
2.5 V, and then sits printing state updates roughly once a second — plus a
``[downloaded]`` line once a minute, when the relay's periodic upload
lands in the databank and the client pulls it back down.

Turning on encryption and the network password
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Neither of the examples above uses CURVE encryption or the shared network
password — both are entirely opt-in. To run the fully-featured example
with both switched on:

.. code-block:: bash

   # once, to generate each keypair — see architecture.rst for why there's
   # only one *server* keypair for the whole mesh, but any number of
   # client keypairs
   lm-keygen --format env      # prints ZMQ_PUBLICKEY / ZMQ_SECRETKEY

``lm-keygen`` always prints the generic names ``ZMQ_PUBLICKEY`` /
``ZMQ_SECRETKEY`` — it doesn't know which role you're generating for, so
you export them under the role-specific name yourself:

.. code-block:: bash

   # on every node's shell (broker, bank, relay, client alike) — this
   # ONE keypair is shared by every server socket in the mesh
   export ZMQ_SERVER_PUBLICKEY="<public from a lm-keygen run>"
   export ZMQ_SERVER_SECRETKEY="<secret from the same run>"

   # on relay, bank, and client shells only — this can be a second,
   # different keypair per node, since nothing checks it against an
   # allowlist (see architecture.rst)
   export ZMQ_CLIENT_PUBLICKEY="<public from a second lm-keygen run>"
   export ZMQ_CLIENT_SECRETKEY="<secret from the same run>"

   # the broker will prompt for this at boot; every other node needs the
   # identical value exported before it starts
   export LMH_NETWORK_PASSWORD=...

   python run_broker.py --toml labmesh.toml
   # ... then the other three nodes, each in the same shell environment

:doc:`architecture` covers exactly which sockets each of these env vars
affects, what's encrypted, what's password-checked, and what neither one
covers.
