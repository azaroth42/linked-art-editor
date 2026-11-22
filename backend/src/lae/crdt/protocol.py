"""Byte-level helpers for the y-sync and awareness protocols that ``pycrdt`` does not expose.

Three things live here, all pure functions:

- the awareness update codec (``decode_awareness`` / ``encode_awareness``), which the room needs
  to stamp the authenticated identity on every presence state and to remove the states of a
  closed socket (S2 findings 4 and 5);
- ``check_update``, which rejects bytes that are not a Yjs update *before* they are persisted
  (a row that cannot be decoded would otherwise fail every later rehydration);
- ``is_integrated``, which tells whether a document has integrated the whole of an update. An
  update that arrives before something it depends on is kept by yrs as *pending*, and
  ``Doc.get_update()`` leaves pending structs and pending deletes out (measured on the pinned
  pycrdt 0.14.13). Compaction therefore must not replace such an update by ``Doc.get_update()``;
  ``lae.crdt.rooms`` keeps it and merges it into the compacted state at the update level.

Formats (Yjs update v1, lib0 encoding): a var-uint is 7 bits per byte, least significant group
first, high bit set on every byte but the last. A state vector is ``n, (client, clock) * n``.
An update is ``<struct blocks>, <delete set>``; when it has no struct block it starts with the
byte ``0`` and the delete set follows: ``n, (client, m, (clock, length) * m) * n``.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pycrdt

EMPTY_UPDATE: Final = b"\x00\x00"
"""The update that changes nothing (no struct block, empty delete set)."""

AwarenessEntry = tuple[int, int, dict[str, Any] | None]
"""``(client id, clock, state)``; ``state`` is ``None`` when the client is leaving."""


class ProtocolError(ValueError):
    """A message, update or awareness payload that cannot be decoded."""


class _Reader:
    """Sequential var-uint / var-string reader over one payload."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def var_uint(self) -> int:
        value = 0
        shift = 0
        while True:
            if self.pos >= len(self.data) or shift > 63:
                raise ProtocolError("truncated or oversized var-uint")
            byte = self.data[self.pos]
            self.pos += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value
            shift += 7

    def var_string(self) -> str:
        length = self.var_uint()
        end = self.pos + length
        if end > len(self.data):
            raise ProtocolError("truncated string")
        try:
            text = self.data[self.pos : end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("string is not UTF-8") from exc
        self.pos = end
        return text

    @property
    def done(self) -> bool:
        return self.pos >= len(self.data)


def _var_uint(value: int) -> bytes:
    return pycrdt.write_var_uint(value)


def frame_payload(frame: bytes) -> bytes:
    """Return the single length-prefixed payload of a frame body (the part after the type bytes)."""
    reader = _Reader(frame)
    length = reader.var_uint()
    end = reader.pos + length
    if end > len(frame):
        raise ProtocolError("truncated message")
    return frame[reader.pos : end]


def decode_awareness(payload: bytes) -> list[AwarenessEntry]:
    """Decode an awareness update into ``(client id, clock, state)`` entries.

    A state that is not a JSON object (``null`` included) is returned as ``None``: the protocol
    only defines objects and ``null``, and ``null`` removes the client.
    """
    reader = _Reader(payload)
    entries: list[AwarenessEntry] = []
    for _ in range(reader.var_uint()):
        client_id = reader.var_uint()
        clock = reader.var_uint()
        raw = reader.var_string()
        try:
            state = json.loads(raw) if raw else None
        except ValueError as exc:
            raise ProtocolError("awareness state is not JSON") from exc
        entries.append((client_id, clock, state if isinstance(state, dict) else None))
    return entries


def encode_awareness(entries: list[AwarenessEntry]) -> bytes:
    """Encode ``(client id, clock, state)`` entries as an awareness update."""
    parts = [_var_uint(len(entries))]
    for client_id, clock, state in entries:
        text = json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        parts += [_var_uint(client_id), _var_uint(clock), _var_uint(len(text)), text]
    return b"".join(parts)


def decode_state_vector(state: bytes) -> dict[int, int]:
    """Decode a state vector to ``{client id: clock}`` (the next clock expected per client)."""
    reader = _Reader(state)
    return {reader.var_uint(): reader.var_uint() for _ in range(reader.var_uint())}


def check_update(update: bytes) -> None:
    """Raise :class:`ProtocolError` unless ``update`` decodes as a Yjs update (v1)."""
    try:
        pycrdt.merge_updates(update)
    except Exception as exc:  # yrs reports every decoding failure as ValueError
        raise ProtocolError(f"not a Yjs update: {exc}") from exc


def is_integrated(doc: pycrdt.Doc[Any], update: bytes) -> bool:
    """Return whether ``doc`` holds everything in ``update`` (nothing of it is pending).

    True when every struct of the update is below the document's state vector and every range
    of its delete set targets structs the document has. Errs on the side of ``False``.
    """
    state = doc.get_state()
    try:
        rest = pycrdt.get_update(update, state)
        if not rest or rest[0] != 0:
            return False  # structs beyond the document's state: waiting for a dependency
        known = decode_state_vector(state)
        reader = _Reader(rest)
        reader.var_uint()  # the zero struct-block count
        for _ in range(reader.var_uint()):
            client_id = reader.var_uint()
            for _ in range(reader.var_uint()):
                clock = reader.var_uint()
                length = reader.var_uint()
                if clock + length > known.get(client_id, 0):
                    return False  # deletes something the document does not have yet
    except Exception:
        return False
    return True
