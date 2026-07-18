labmesh
=======

**labmesh** is a small, dependency-light `ZeroMQ <https://zeromq.org/>`_ mesh
networking library for lab-instrument automation. It is built entirely on
``asyncio`` and ``zmq.asyncio``, and its only runtime dependency is
`pyzmq <https://pyzmq.readthedocs.io/>`_.

A labmesh network has exactly one **broker** and any number of **relay**,
**databank**, and **client** nodes:

.. list-table::
   :widths: 16 84
   :header-rows: 0

   * - **broker**
     - The single directory + presence server for the mesh. Tracks which
       relays and databanks exist and forwards their published updates to
       subscribers.
   * - **relay**
     - Wraps a driver object (e.g. an instrument driver) and exposes its
       public methods to the network as remote procedure calls; also
       publishes periodic state updates.
   * - **databank**
     - Stores datasets uploaded by relays and serves them back out to
       clients on request.
   * - **client**
     - The consumer-facing node: looks relays and databanks up through the
       broker, calls relay methods directly, subscribes to state/dataset
       updates, and downloads datasets.

Where to start
---------------

- New to labmesh? Start with :doc:`usage` for a minimal working network and
  a fully-featured example with data upload and CURVE/password security.
- Want to know exactly what happens on the wire — what's encrypted, what's
  password-checked, and when? Read :doc:`architecture`.
- Looking for a specific class or method? See :doc:`api`.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   installation
   usage
   architecture
   api
