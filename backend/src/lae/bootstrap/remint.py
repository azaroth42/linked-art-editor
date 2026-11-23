"""Re-minting bulk-loaded records and rewriting the references to them (ADR-0030).

Every record of this installation has the URI ``<base_uri><uuid>`` (decision A6). A record that
arrives with any other URI is *re-minted*: ``remint`` gives it a new ``<base_uri><uuidv7>`` URI,
moves the internal node IRIs it may already carry to the new namespace, and appends the source
URI to the root ``equivalent`` as a reference (once). The source URI becomes a row of
``record_aliases``.

Other records still point at the source URI. ``rewrite_references`` replaces every ``id`` that
is an alias with the URI of the record it belongs to, at any depth: plain references, references
inside embedded nodes, and the ``equivalent`` entries of *other* records. Three places are never
rewritten:

- ``@context`` (opaque);
- ``access_point``: its ``id`` is an address to fetch, not a reference to a record;
- an alias of the record itself. The root ``equivalent`` entry that ``remint`` adds is one, and
  must stay; anywhere else ``remint`` has already replaced the record's source URI, before the
  record was checked. So this pass never introduces the record's own URI, which is what lets
  ``lae.bootstrap.rewrite`` argue that it cannot make a record unexpandable.

Both functions return new documents and leave their input alone. Neither touches the database;
``lae.bootstrap.load`` and ``lae.bootstrap.rewrite`` supply the URIs and the alias map.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final
from uuid import UUID

from lae.ids import NODE_PATH, is_internal_node_iri, record_id_from_uri, record_uri

EQUIVALENT: Final = "equivalent"
_ID_KEYS: Final = ("id", "@id")
UNREWRITTEN_KEYS: Final = frozenset({"@context", "access_point"})
"""Keys whose values keep every ``id`` as it is (module docstring)."""


def local_record_id(uri: str, base_uri: str) -> UUID | None:
    """Return the record id if ``uri`` is exactly ``<base_uri><uuid>``, else ``None``.

    A node IRI of a local record is not a record URI, so it gives ``None`` too.
    """
    record_id = record_id_from_uri(uri, base_uri=base_uri)
    if record_id is None or uri != record_uri(record_id, base_uri=base_uri):
        return None
    return record_id


def _entry_id(entry: Any) -> str | None:
    if isinstance(entry, dict):
        for key in _ID_KEYS:
            if isinstance(entry.get(key), str):
                value: str = entry[key]
                return value
    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _rekey(value: Any, source_uri: str, uri: str) -> Any:
    """Move the record's own ids below the root from the ``source_uri`` namespace to ``uri``."""
    if isinstance(value, list):
        return [_rekey(item, source_uri, uri) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in UNREWRITTEN_KEYS:
            out[key] = item
        elif key in _ID_KEYS and isinstance(item, str):
            if item == source_uri:
                out[key] = uri
            elif is_internal_node_iri(item, source_uri):
                out[key] = uri + item[len(source_uri) :]
            else:
                out[key] = item
        else:
            out[key] = _rekey(item, source_uri, uri)
    return out


def remint(document: Mapping[str, Any], source_uri: str, uri: str) -> dict[str, Any]:
    """Return ``document`` as the record ``uri``; its root ``id`` was ``source_uri``.

    - The root ``id`` becomes ``uri``.
    - An internal node IRI ``<source_uri>/node/<uuidv7>`` (the record was exported by an editor,
      or made by the Spike S6 generator) becomes ``<uri>/node/<uuidv7>``, so embedded nodes stay
      the record's own instead of turning into nodes with a foreign id (ADR-0021).
    - A reference to ``source_uri`` below the root (a record that refers to itself) becomes a
      reference to ``uri``.
    - ``{"id": source_uri, "type": <root type>}`` is appended to the root ``equivalent``, unless
      an entry with that ``id`` is there already. A single ``equivalent`` object becomes a list.
    """
    out: dict[str, Any] = {}
    for key, item in document.items():
        if key in UNREWRITTEN_KEYS:
            out[key] = item
        elif key == "id":
            out[key] = uri
        elif key == EQUIVALENT:
            out[key] = [
                entry if _entry_id(entry) == source_uri else _rekey(entry, source_uri, uri)
                for entry in _as_list(item)
            ]
        else:
            out[key] = _rekey(item, source_uri, uri)
    entries: list[Any] = out.setdefault(EQUIVALENT, [])
    if all(_entry_id(entry) != source_uri for entry in entries):
        reference: dict[str, Any] = {"id": source_uri}
        if isinstance(document.get("type"), str):
            reference["type"] = document["type"]
        entries.append(reference)
    return out


def _walk_targets(value: Any, own_nodes: str) -> Iterator[str]:
    if isinstance(value, list):
        for item in value:
            yield from _walk_targets(item, own_nodes)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in UNREWRITTEN_KEYS:
                continue
            if key in _ID_KEYS:
                if isinstance(item, str) and not item.startswith(own_nodes):
                    yield item
            else:
                yield from _walk_targets(item, own_nodes)


def reference_targets(document: Mapping[str, Any], uri: str) -> set[str]:
    """Return every ``id`` below the root that ``rewrite_references`` could rewrite.

    The record's own node IRIs (anything under ``<uri>/node/``) are left out: they are most of
    the ids of a record and can never be an alias worth looking up.
    """
    own_nodes = uri + NODE_PATH
    found: set[str] = set()
    for key, item in document.items():
        if key not in UNREWRITTEN_KEYS and key not in _ID_KEYS:
            found.update(_walk_targets(item, own_nodes))
    return found


class _Rewriter:
    def __init__(self, uri: str, aliases: Mapping[str, str]) -> None:
        self.uri = uri
        self.aliases = aliases
        self.count = 0

    def walk(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in UNREWRITTEN_KEYS:
                out[key] = item
            elif key in _ID_KEYS and isinstance(item, str):
                target = self.aliases.get(item)
                if target is None or target == item or target == self.uri:
                    out[key] = item
                else:
                    out[key] = target
                    self.count += 1
            else:
                out[key] = self.walk(item)
        return out


def rewrite_references(
    document: Mapping[str, Any], uri: str, aliases: Mapping[str, str]
) -> tuple[dict[str, Any], int]:
    """Return ``(document with aliases replaced, number of ids replaced)``.

    ``aliases`` maps an alias URI to the URI of its record; ``uri`` is the URI of the record
    ``document`` is. See the module docstring for what is and is not rewritten.
    """
    rewriter = _Rewriter(uri, aliases)
    out: dict[str, Any] = {}
    for key, item in document.items():
        if key in UNREWRITTEN_KEYS or key in _ID_KEYS:
            out[key] = item
        else:
            out[key] = rewriter.walk(item)
    return out, rewriter.count
