"""CRDT rooms and draft persistence (Phase 1 Step 1.8; spec §4.1-4.2; decisions A4, B1, B5, B6).

- ``rooms``: ``RoomManager`` (the interface), ``InMemoryRoomManager``, ``Room``, ``Snapshot``.
  Read its module docstring for the invariants: one hydration per draft epoch, persist before
  broadcast, lossless compaction, no room dropped while it has sockets.
- ``store``: the SQL on ``crdt_drafts`` / ``crdt_updates`` / ``records.draft_epoch``.
  ``store.delete_draft`` is the only correct way to delete a draft (ADR-0027).
- ``protocol``: awareness codec and update inspection.
- ``instance``: the advisory lock that keeps ``crdt-ws`` a single process per database.
- ``server``: the ``crdt-ws`` ASGI application (``/ws/rooms/{record_id}`` and ``/internal/``)
  and ``main(settings)`` for ``lae crdt-ws``. Its docstring is the wire contract for the editor
  (Step 1.17): query parameters, close codes, read-only behaviour.
- ``client``: ``CrdtWsClient``, what every other process uses.
- ``errors``: the ``LAE-CRDT-NNN`` codes.

Nothing is re-exported here: ``lae.crdt.client`` imports FastAPI and ``lae.crdt.rooms`` imports
``pycrdt``; import the module you need.
"""
