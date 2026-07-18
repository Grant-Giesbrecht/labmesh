Installation
============

labmesh requires Python 3.10+ and has one runtime dependency,
`pyzmq <https://pyzmq.readthedocs.io/>`_.

.. code-block:: bash

   pip install -e .

installed from a checkout of the repository, or from PyPI once published:

.. code-block:: bash

   pip install labmesh

.. note::
   On Python 3.10, labmesh's TOML config loader (:func:`labmesh.util.read_toml_config`)
   falls back to the third-party ``tomli`` package (stdlib ``tomllib`` is
   only available on 3.11+). If you're on 3.10, install it alongside
   labmesh: ``pip install tomli``.

This also installs the ``lm-keygen`` console script, used to generate CURVE
keypairs for encrypting a node's sockets — see :ref:`curve-encryption` for
how and when to use it.

Verifying the install
----------------------

labmesh has no test suite; the way to confirm a working install is to run
the minimal example end to end. See :doc:`usage`.
