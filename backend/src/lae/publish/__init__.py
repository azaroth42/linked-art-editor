"""Publish: the one place where a draft becomes a published version (spec §4.1; decision B8).

- ``service``: ``publish`` (the transaction) and ``discard``. Read its module docstring before
  changing anything: it lists the invariants and where each is enforced.
- ``errors``: the ``LAE-PUB-NNN`` codes and ``PublishError``.

The HTTP endpoints are in ``lae.api.publish``. Nothing is re-exported here: ``service`` imports
the ``crdt-ws`` client and the validation engine; import the module you need.
"""
