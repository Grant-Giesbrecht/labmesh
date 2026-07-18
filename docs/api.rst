API Reference
==============

This page is generated from the docstrings and signatures in the source.
For how these pieces fit together, see :doc:`architecture`; for worked
examples, see :doc:`usage`.

Broker
-------

.. autoclass:: labmesh.DirectoryBroker
   :members:
   :undoc-members:

Relay
------

.. autoclass:: labmesh.RelayAgent
   :members:
   :undoc-members:

.. autofunction:: labmesh.relay.upload_dataset

Databank
---------

.. autoclass:: labmesh.DataBank
   :members:
   :undoc-members:

Client
-------

.. autoclass:: labmesh.DirectorClientAgent
   :members:
   :undoc-members:

.. autoclass:: labmesh.RelayClient
   :members:
   :undoc-members:

.. autoclass:: labmesh.BankClient
   :members:
   :undoc-members:

Security
---------

.. autofunction:: labmesh.security.generate_curve_keypair

.. autofunction:: labmesh.util.network_password

.. autofunction:: labmesh.util.check_password

.. autofunction:: labmesh.util.prompt_network_password

Utilities
----------

.. autofunction:: labmesh.util.read_toml_config

.. autofunction:: labmesh.util.dumps

.. autofunction:: labmesh.util.loads

.. autofunction:: labmesh.util.ensure_windows_selector_loop

.. autofunction:: labmesh.util.env_int
