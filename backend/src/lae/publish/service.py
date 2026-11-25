"""The publish transaction and draft discard (Phase 1 Step 1.10; spec §3.1-§3.3, §4.1).

``publish`` turns the live draft of a record into its next published version, together with
every never-published record the draft refers to (its **dependent stubs**, transitively), in one
PostgreSQL transaction. ``discard`` throws a draft away. Both are called by ``lae.api.publish``
and may be called by any other process that has a session factory and a ``CrdtWsClient``.

What ``publish`` does, in order:

1. **Collect** (``_collect``). The snapshot of the record's draft comes from ``crdt-ws``. Every
   reference in it whose ``id`` is a ``record_aliases.alias_uri`` is first rewritten to the URI
   of that record (``_resolve_aliases``, ADR-0030); the result is the document that is
   validated and stored. Every reference in it whose target is a ``records`` row at
   ``version = 0`` names a dependent stub; its snapshot is fetched and resolved too, and so on
   until no new one turns up. The set is the **bundle**.
2. **Validate** (``_validate``). Each resolved snapshot goes through the pipeline of
   ``lae.validation.validate`` (rewriting an ``id`` moves no diagnostic, so codes and paths
   agree with what ``POST /api/records/{id}/draft/validate`` showed the editor). A violation
   anywhere refuses the whole bundle; every warning must be among the request's accepted
   ``(record, code, path)`` triples.
3. **Write** (``_write_bundle``), one transaction: lock every ``records`` row of the bundle
   ``FOR UPDATE`` in UUID order; then, in the same order, require of every draft that it is
   still the snapshot that was validated; then write each record (``_write_record``): the
   revision of a bulk-loaded previous version if it has none, ``records``, the new revision,
   the outbox event, ``entity_labels`` (with the metatype harvest of ADR-0032), the
   resolution of the record's open ``LAE-SCHEMA-*`` QA findings (ADR-0030), and the deletion
   of the draft.
4. **Close** the rooms in ``crdt-ws`` (best effort, after the commit).

``discard`` never discards a draft it was not asked about (issue #178). It reads
``records.draft_epoch`` first, which is the epoch of the record's draft if it has one, and asks
``crdt-ws`` to discard the draft of exactly that epoch. A caller that names the epoch it was
looking at (``draft_epoch``) is refused with 409 ``LAE-CRDT-007`` when the record has moved on
to another one, and so is any caller whose draft was published while the request was under way:
the draft somebody has opened since, with whatever was typed into it, is left alone.

Invariants, and where each one is enforced:

- **All or nothing.** Everything in step 3 is one transaction. Any exception rolls it back; the
  drafts, the records and every other table are then exactly as before.
- **Nothing is published that was not validated.** ``lae.crdt.store.draft_unchanged`` compares
  each draft with its snapshot (``draft_epoch`` and ``last_update_id``) *inside* the transaction
  and keeps the draft row locked, so no update can be appended before the commit. If a draft
  changed, was published or was discarded in between, the answer is 409 ``LAE-PUB-004`` and
  nothing is written; the edit that arrived is still in the draft. There is no automatic retry:
  a retry would publish content the caller has not seen diagnostics for.
- **No edit is lost.** A draft is deleted only after that check, and only through
  ``lae.crdt.store.delete_draft`` (ADR-0027). An update that arrives while the transaction holds
  its locks waits for the commit, finds its draft gone, and is refused to its client (close
  code 4409); it was never acknowledged.
- **Two publishes of one draft produce one version.** The second waits for the row lock, then
  finds the draft gone (or replaced by a draft of the next epoch) and gets ``LAE-PUB-004``.
- **One lock order: ``records`` rows in UUID order, then ``crdt_drafts`` rows in UUID order.**
  ``crdt-ws`` takes the ``records`` row before the draft row too (``lae.crdt.store``), and
  every transaction that locks several records must use UUID order (spec §3.4), so publish can
  wait but cannot deadlock on those rows.
- **A deadlock on other rows costs nobody a publish** (issue #178). The transaction also writes
  ``entity_labels``: the row of each record it publishes and, in the metatype harvest, rows of
  concepts those records cite, which other publishes (of those concepts) and other writers of
  the table lock in no agreed order. PostgreSQL resolves such a cycle by aborting one
  transaction (SQLSTATE 40P01). That transaction has written nothing, so the write step is run
  again, ``WRITE_ATTEMPTS`` times in all: a fresh transaction that takes every lock and
  repeats every check, and therefore still publishes exactly the validated snapshot or
  refuses with the usual code.
- **A draft is published onto the version it was hydrated from.** ``records.version`` must
  equal the snapshot's ``base_version`` (409 ``LAE-PUB-006`` otherwise). Today nothing changes
  a record under a live draft; the check is there for the day something does (a merge that
  rewrites references, Phase 2).
- **``draft_epoch`` goes up by exactly one per published record** (decision B6): publish
  increments it and ``delete_draft`` then finds it already beyond the deleted draft's epoch.
- **Every version from 1 to ``records.version`` has a ``record_revisions`` row once the record
  has been published here.** A bulk-loaded record has none for its loaded version (decision
  A9), so its first publish inserts that row as well, from the ``records.data`` it is about to
  replace: ``/id/{uuid}?version=1`` keeps resolving and the sync worker finds the previous
  JSON-LD and triples. That row has no publisher (``published_by`` is NULL, migration 0009)
  and the time the loaded version was written.
- **What is stored is what was expanded.** The N-Triples of a revision are expanded from the
  exact ``data`` written to ``records`` and to the revision's JSON-LD snapshot, after stub
  normalization, in the same step. ``records.etag`` and the snapshot are both made from
  ``canonical_json(data)``, so a version keeps its entity tag when it stops being current.

Outbox events (the contract with the sync worker, Step 1.11). One per published record:
``kind = 'publish'``, ``to_version`` the new version, ``from_version`` the previous one, or
**NULL for a first publish** (there is nothing in QLever to retract). ``payload`` is
``{"context_version": <of to_version>, "drift_check": bool}``. ``drift_check`` is true only for
the first edit of a bulk-loaded record whose triples in QLever may differ from the ones stored
for ``from_version`` (ADR-0007, decision A13): the index was built with another context or
expander version than the current ones, the build's versions are not recorded in
``sync_state``, or the previous JSON-LD cannot be expanded at all (the stored N-Triples of
``from_version`` are then empty). The worker must then retract with ``DELETE WHERE`` over
``owned_subjects`` of ``from_version``'s JSON-LD instead of a computed diff. Both revisions
always exist when the event is visible.

Things this module deliberately does not do:

- It never reads or writes ``crdt_drafts`` / ``crdt_updates`` itself (ADR-0027).
- It does not skip a publish whose content equals the published version: the caller asked for
  a version, and the worker's diff is then empty.
- It does not write QA findings for unresolved references (decision A11); the QA sweeper does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Text, any_, bindparam, exists, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lae.aliases import resolve_aliases
from lae.api.errors import ApiError
from lae.api.records import RECORD_NOT_FOUND, record_etag
from lae.auth import CurrentUser
from lae.auth.errors import permission_denied
from lae.bootstrap.common import BOOTSTRAP_CONTEXT_VERSION, BOOTSTRAP_EXPANDER_VERSION
from lae.crdt import errors as crdt_errors
from lae.crdt import store
from lae.crdt.client import CrdtWsClient
from lae.crdt.rooms import Snapshot
from lae.db.models import (
    OutboxEvent,
    OutboxEventKind,
    QaFinding,
    QaFindingStatus,
    Record,
    RecordRevision,
    RecordStatus,
    SyncState,
)
from lae.labels.normalize import known_reference_ids, normalize_refs
from lae.labels.refs import reference_ids, rewrite_references
from lae.labels.upsert import upsert_entity_labels_for_record
from lae.logging import bind_log_context, get_logger
from lae.projection import canonical_json
from lae.publish import errors
from lae.rdf import (
    EXPANDER_VERSION,
    ExpansionError,
    canonical_ntriples,
    compress,
    expand_to_ntriples,
)
from lae.scheduler.reaper import delete_unpublished_record
from lae.settings import Settings
from lae.validation import (
    Diagnostic,
    Diagnostics,
    ReferenceFacts,
    load_reference_facts,
    validate_document,
)
from lae.validation.expansion import expansion_diagnostic
from lae.validation.schema import SCHEMA_CODE_PREFIX

PUBLISH_PERMISSION: Final = "publish"

WRITE_ATTEMPTS: Final = 3
"""How often ``publish`` runs its write transaction when PostgreSQL aborts it as a deadlock."""

DEADLOCK_DETECTED: Final = "40P01"
"""SQLSTATE of a transaction PostgreSQL aborted to break a lock cycle; nothing of it is kept."""

_UNPUBLISHED_TARGETS: Final = select(Record.id, Record.uri).where(
    Record.uri == any_(bindparam("uris", type_=ARRAY(Text))), Record.version == 0
)

_RESOLVABLE_FINDINGS: Final = (QaFindingStatus.OPEN, QaFindingStatus.CLAIMED)
"""``qa_findings.status`` values a publish resolves (for ``LAE-SCHEMA-*`` codes only)."""

logger = get_logger(__name__)


class AcceptedWarning(BaseModel):
    """One warning the caller has seen and accepts (decision C2): its ``code`` and ``path``.

    ``record_id`` names the record of the bundle the warning belongs to; ``None`` is the record
    being published. A path is a position in the document, so the same ``code`` and ``path``
    in a dependent stub is a different warning.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    path: str
    record_id: UUID | None = None


class PublishResult(BaseModel):
    """What ``publish`` did."""

    version: int
    """The new ``records.version`` of the record that was published."""
    published: list[UUID]
    """Every record published by the transaction: the record itself, then its dependent stubs."""
    draft_epoch: int
    """The record's new ``records.draft_epoch``: the epoch its next draft will have."""


class DiscardResult(BaseModel):
    """What ``discard`` did."""

    id: UUID
    deleted: bool
    """True when the record had never been published and its row is gone."""
    draft_epoch: int | None
    """``records.draft_epoch`` afterwards; ``None`` when the record was deleted."""


@dataclass(slots=True)
class _Document:
    """One record of the bundle: its snapshot, its resolved document and its diagnostics.

    ``jsonld`` is ``snapshot.jsonld`` with aliases resolved (``_resolve_aliases``): the
    document that is validated and, after stub normalization, stored.
    """

    record_id: UUID
    uri: str
    snapshot: Snapshot
    jsonld: dict[str, Any]
    diagnostics: Diagnostics | None = None

    @property
    def warnings(self) -> list[Diagnostic]:
        assert self.diagnostics is not None
        return [item for item in self.diagnostics.items if item.severity == "warning"]


@dataclass(frozen=True, slots=True)
class _Rendered:
    """The stored forms of one version of a record."""

    jsonld_zstd: bytes
    ntriples_zstd: bytes
    etag: str


def _record_not_found() -> ApiError:
    return ApiError(404, RECORD_NOT_FOUND)


def draft_superseded(record_id: UUID) -> ApiError:
    """409 ``LAE-CRDT-007``: the draft the caller meant was published or discarded meanwhile.

    The answer of ``discard`` (and of revert, ``lae.api.revisions``) when acting would touch a
    draft other than the one the caller was looking at. Nothing was discarded by this request
    beyond, at most, the draft it named; the record's current draft is as it was.
    """
    return ApiError(409, crdt_errors.DRAFT_SUPERSEDED, {"record_id": str(record_id)})


async def _version_and_epoch(
    sessions: async_sessionmaker[AsyncSession], record_id: UUID
) -> tuple[int, int] | None:
    """Return ``(records.version, records.draft_epoch)`` as committed now; ``None``: no record."""
    async with sessions() as session:
        row = (
            await session.execute(
                select(Record.version, Record.draft_epoch).where(Record.id == record_id)
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1])


def _require_publish(user: CurrentUser) -> None:
    if not user.has(PUBLISH_PERMISSION):
        raise permission_denied([PUBLISH_PERMISSION], [PUBLISH_PERMISSION])


# -- step 1: collect ---------------------------------------------------------------------------


async def _resolve_aliases(session: AsyncSession, jsonld: dict[str, Any]) -> dict[str, Any]:
    """Return ``jsonld`` with every reference to an alias pointing at its record's URI.

    ``record_aliases`` keeps the URIs records had before ``lae bootstrap load`` re-minted them
    (ADR-0030); a draft can still carry one (pasted, or from a template). Only references are
    rewritten (``lae.labels.refs``): the root ``id`` is never one. Returns ``jsonld`` itself
    when it refers to no alias.
    """
    found = await resolve_aliases(session, reference_ids(jsonld))
    if not found:
        return jsonld
    rows = await session.execute(
        select(Record.id, Record.uri).where(Record.id.in_(set(found.values())))
    )
    uri_of = {row[0]: row[1] for row in rows}
    targets = {alias: uri_of[target] for alias, target in found.items() if target in uri_of}

    def to_record(reference: dict[str, Any]) -> dict[str, Any]:
        target = targets.get(reference["id"])
        if target is not None:
            reference["id"] = target
        return reference

    return rewrite_references(jsonld, to_record)


async def _collect(
    record_id: UUID, session: AsyncSession, crdt: CrdtWsClient
) -> dict[UUID, _Document]:
    """Return the bundle: the record's draft and the drafts of its dependent stubs, transitively.

    Raises 404 for an unknown record, ``LAE-PUB-005`` for a deleted or merged one and
    ``LAE-PUB-001`` (naming the record) for the record or a dependent stub without a draft.
    """
    row = (
        await session.execute(select(Record.uri, Record.status).where(Record.id == record_id))
    ).one_or_none()
    if row is None:
        raise _record_not_found()
    uri, status = row
    if status is not RecordStatus.ACTIVE:
        raise errors.not_publishable(record_id, status.value)
    snapshot = await crdt.snapshot(record_id)
    if snapshot is None:
        raise errors.no_draft(record_id)
    jsonld = await _resolve_aliases(session, snapshot.jsonld)
    bundle = {record_id: _Document(record_id, uri, snapshot, jsonld)}
    seen: set[str] = {uri}
    pending = [jsonld]
    while pending:
        targets = reference_ids(pending.pop()) - seen
        seen |= targets
        if not targets:
            continue
        rows = await session.execute(_UNPUBLISHED_TARGETS, {"uris": sorted(targets)})
        for stub_id, stub_uri in sorted((row[0], row[1]) for row in rows):
            if stub_id in bundle:
                continue
            stub_snapshot = await crdt.snapshot(stub_id)
            if stub_snapshot is None:
                raise errors.no_draft(stub_id)
            stub_jsonld = await _resolve_aliases(session, stub_snapshot.jsonld)
            bundle[stub_id] = _Document(stub_id, stub_uri, stub_snapshot, stub_jsonld)
            pending.append(stub_jsonld)
    return bundle


# -- step 2: validate --------------------------------------------------------------------------


async def _validate(
    bundle: Mapping[UUID, _Document], session: AsyncSession, context_version: int
) -> None:
    """Validate every document of the bundle and store its diagnostics on it.

    The pipeline is ``lae.validation.validate``'s. One thing differs from validating a draft on
    its own: the class of a bundle member is the ``type`` of its draft, not what a reference to
    it claims, because that is what ``entity_labels`` will say once the bundle is published.
    """
    targets: set[str] = set()
    for document in bundle.values():
        targets |= reference_ids(document.jsonld)
    known = await load_reference_facts(session, targets)
    types = dict(known.types)
    for document in bundle.values():
        declared = document.jsonld.get("type")
        if isinstance(declared, str):
            types[document.uri] = declared
    facts = ReferenceFacts(types, known.deleted)
    for document in bundle.values():
        document.diagnostics = await asyncio.to_thread(
            validate_document,
            document.jsonld,
            document.uri,
            facts,
            context_version=context_version,
        )


def _check_diagnostics(
    record_id: UUID, bundle: Mapping[UUID, _Document], accepted: Collection[AcceptedWarning]
) -> None:
    """Raise ``LAE-PUB-003`` for a violation, else ``LAE-PUB-002`` for an unaccepted warning."""
    violations: dict[UUID, list[Diagnostic]] = {}
    for document in bundle.values():
        assert document.diagnostics is not None
        if not document.diagnostics.ok:
            violations[document.record_id] = document.diagnostics.items
    if violations:
        raise errors.validation_failed(violations)
    accepted_keys = {(item.record_id or record_id, item.code, item.path) for item in accepted}
    warnings = {doc.record_id: doc.warnings for doc in bundle.values() if doc.warnings}
    unaccepted = sorted(
        {
            (found_in, item.code, item.path)
            for found_in, items in warnings.items()
            for item in items
            if (found_in, item.code, item.path) not in accepted_keys
        }
    )
    if unaccepted:
        raise errors.warnings_not_accepted(warnings, unaccepted)


# -- step 3: write -----------------------------------------------------------------------------


def _render(data: dict[str, Any], uri: str, context_version: int) -> _Rendered:
    """Return the stored forms of ``data``; raises ``ExpansionError`` (CPU-bound, synchronous)."""
    canonical = canonical_json(data)
    triples = expand_to_ntriples(data, context_version, record_uri=uri)
    return _Rendered(
        jsonld_zstd=compress(canonical),
        ntriples_zstd=compress(canonical_ntriples(triples)),
        etag=record_etag(data),
    )


def _render_loaded(data: dict[str, Any], uri: str, context_version: int) -> tuple[_Rendered, bool]:
    """Return the stored forms of a bulk-loaded version and whether it could be expanded.

    A record that cannot be expanded has no triples in an index built by this expander; its
    revision then stores no triples, and the caller sets ``drift_check``.
    """
    try:
        return _render(data, uri, context_version), True
    except ValueError:  # ExpansionError, or a line that is not canonical N-Triples
        empty = compress(canonical_ntriples(()))
        return _Rendered(compress(canonical_json(data)), empty, record_etag(data)), False


async def _index_matches(session: AsyncSession, context_versions: Collection[int]) -> bool:
    """Return whether the QLever index was built with exactly these context and expander versions.

    ``sync_state`` holds what the last bulk build used (``lae.bootstrap``); a missing key means
    nobody knows, which counts as a difference (ADR-0007).
    """
    rows = await session.execute(
        select(SyncState.key, SyncState.value).where(
            SyncState.key.in_((BOOTSTRAP_CONTEXT_VERSION, BOOTSTRAP_EXPANDER_VERSION))
        )
    )
    state: dict[str, Any] = {row[0]: row[1] for row in rows}
    built_with = state.get(BOOTSTRAP_CONTEXT_VERSION)
    return (
        built_with is not None
        and all(version == built_with for version in context_versions)
        and state.get(BOOTSTRAP_EXPANDER_VERSION) == EXPANDER_VERSION
    )


async def _insert_loaded_revision(
    session: AsyncSession, record: Record, loaded_at: datetime, current_context_version: int
) -> bool:
    """Insert the revision of the bulk-loaded version ``record`` is at; return ``drift_check``.

    Called with the row still holding the loaded ``data``. The revision stores that JSON-LD and
    its triples under the context version it was loaded with.
    """
    rendered, expanded = await asyncio.to_thread(
        _render_loaded, record.data, record.uri, record.context_version
    )
    session.add(
        RecordRevision(
            record_id=record.id,
            version=record.version,
            ntriples_zstd=rendered.ntriples_zstd,
            jsonld_zstd=rendered.jsonld_zstd,
            context_version=record.context_version,
            authors=[],
            published_by=None,
            summary="",
            accepted_warnings=[],
            created_at=loaded_at,
        )
    )
    await session.flush()
    exact = expanded and await _index_matches(
        session, (record.context_version, current_context_version)
    )
    return not exact


async def _resolve_schema_findings(session: AsyncSession, record_id: UUID, user_id: UUID) -> None:
    """Resolve the record's open or claimed ``LAE-SCHEMA-*`` findings (ADR-0030).

    A bulk load records the JSON Schema failures of the records it loads anyway
    (``lae.bootstrap.findings``). A published version has passed the schema, so those findings
    are settled. Findings with other codes belong to the QA sweeps and are left alone.
    """
    await session.execute(
        update(QaFinding)
        .where(
            QaFinding.record_id == record_id,
            QaFinding.status.in_(_RESOLVABLE_FINDINGS),
            QaFinding.code.startswith(SCHEMA_CODE_PREFIX, autoescape=True),
        )
        .values(status=QaFindingStatus.RESOLVED, resolved_by=user_id, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def _write_record(
    session: AsyncSession,
    record: Record,
    document: _Document,
    *,
    user: CurrentUser,
    summary: str,
    bundle_uris: set[str],
    context_version: int,
    label_language: str,
) -> None:
    """Write the next version of one locked record from its validated snapshot.

    ``record`` is locked ``FOR UPDATE`` and its draft was found unchanged in this transaction.
    ``label_language`` (``Settings.label_language``) chooses the ``entity_labels`` default label.
    """
    snapshot = document.snapshot
    previous_version = record.version
    # Every member of the bundle has an entity_labels row when this transaction commits.
    known = await known_reference_ids(session, document.jsonld) | bundle_uris
    data = normalize_refs(document.jsonld, known)
    try:
        rendered = await asyncio.to_thread(_render, data, record.uri, context_version)
    except ExpansionError as error:
        # Validation expanded the same document; kept as an answer rather than a 500.
        raise errors.validation_failed(
            {record.id: [expansion_diagnostic(error, data, record.uri)]}
        ) from error

    drift_check = False
    if previous_version >= 1:
        has_revision = await session.scalar(
            select(
                exists().where(
                    RecordRevision.record_id == record.id,
                    RecordRevision.version == previous_version,
                )
            )
        )
        if not has_revision:
            drift_check = await _insert_loaded_revision(
                session, record, record.updated_at, context_version
            )

    new_type = data.get("type")
    if isinstance(new_type, str):
        record.type = new_type  # a reclassified draft (Phase 4); the schema checked the value
    record.data = data
    record.version = previous_version + 1
    record.etag = rendered.etag
    record.context_version = context_version
    record.draft_epoch = snapshot.draft_epoch + 1
    session.add(
        RecordRevision(
            record_id=record.id,
            version=record.version,
            ntriples_zstd=rendered.ntriples_zstd,
            jsonld_zstd=rendered.jsonld_zstd,
            context_version=context_version,
            authors=sorted({*snapshot.contributors, user.id}),
            published_by=user.id,
            summary=summary,
            accepted_warnings=[
                {"code": code, "path": path}
                for path, code in sorted({(item.path, item.code) for item in document.warnings})
            ],
        )
    )
    session.add(
        OutboxEvent(
            kind=OutboxEventKind.PUBLISH,
            record_id=record.id,
            from_version=previous_version or None,
            to_version=record.version,
            payload={"context_version": context_version, "drift_check": drift_check},
        )
    )
    await session.flush()
    # Also harvests the metatypes of inline Type references onto existing rows (ADR-0032).
    await upsert_entity_labels_for_record(session, record, label_language)
    await _resolve_schema_findings(session, record.id, user.id)
    epoch = await store.delete_draft(session, record.id)
    if epoch != record.draft_epoch:
        raise RuntimeError(
            f"{record.id}: draft_epoch is {epoch} after the draft was deleted, "
            f"expected {record.draft_epoch}"
        )


async def _write_bundle(
    session: AsyncSession,
    bundle: Mapping[UUID, _Document],
    *,
    user: CurrentUser,
    summary: str,
    context_version: int,
    label_language: str,
) -> dict[UUID, Record]:
    """Lock, re-check and write the whole bundle; the caller's transaction makes it atomic."""
    order = sorted(bundle)
    records: dict[UUID, Record] = {}
    for record_id in order:  # lock order: records, in UUID order
        record = await session.scalar(
            select(Record)
            .where(Record.id == record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            # A never-published record whose draft was discarded since the snapshot.
            raise errors.draft_changed(record_id)
        if record.status is not RecordStatus.ACTIVE:
            raise errors.not_publishable(record_id, record.status.value)
        records[record_id] = record
    for record_id in order:  # then drafts, in the same order
        snapshot = bundle[record_id].snapshot
        if not await store.draft_unchanged(
            session, record_id, snapshot.draft_epoch, snapshot.last_update_id
        ):
            raise errors.draft_changed(record_id)
        record = records[record_id]
        if record.version != snapshot.base_version or record.draft_epoch != snapshot.draft_epoch:
            raise errors.stale_base(record_id, snapshot.base_version, record.version)
    bundle_uris = {record.uri for record in records.values()}
    for record_id in order:
        await _write_record(
            session,
            records[record_id],
            bundle[record_id],
            user=user,
            summary=summary,
            bundle_uris=bundle_uris,
            context_version=context_version,
            label_language=label_language,
        )
    return records


def _is_deadlock(error: DBAPIError) -> bool:
    return getattr(error.orig, "sqlstate", None) == DEADLOCK_DETECTED


# -- the entry points --------------------------------------------------------------------------


async def publish(
    record_id: UUID,
    user: CurrentUser,
    summary: str,
    accepted_warnings: Collection[AcceptedWarning],
    *,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
    settings: Settings,
) -> PublishResult:
    """Publish the draft of ``record_id`` and its dependent stubs in one transaction.

    ``accepted_warnings`` are the warnings the caller was shown and accepts; validation is run
    again here and every warning it finds must be among them, by record, code and exact path.

    Raises ``lae.publish.errors.PublishError`` (``LAE-PUB-001`` to ``-006``), 403
    ``LAE-AUTH-001`` without the ``publish`` permission, 404 ``LAE-REC-001``, and
    ``lae.crdt.client.CrdtWsError`` when the draft cannot be read. After any of them nothing
    has been written and the draft is unchanged.
    """
    _require_publish(user)
    bind_log_context(user_id=user.id, record_id=record_id)
    async with sessions() as session:
        bundle = await _collect(record_id, session, crdt)
        await _validate(bundle, session, settings.context_version)
    _check_diagnostics(record_id, bundle, accepted_warnings)

    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            async with sessions() as session, session.begin():
                records = await _write_bundle(
                    session,
                    bundle,
                    user=user,
                    summary=summary,
                    context_version=settings.context_version,
                    label_language=settings.label_language,
                )
                version = records[record_id].version
                draft_epoch = records[record_id].draft_epoch
            break
        except DBAPIError as error:
            # Rolled back whole by PostgreSQL. The next attempt locks and checks everything
            # again, so it publishes the validated snapshot or fails like a first attempt.
            if not _is_deadlock(error) or attempt == WRITE_ATTEMPTS:
                raise
            logger.warning("publish_deadlock_retried", attempt=attempt)
    # Committed. The rooms are closed on a best-effort basis: a room that outlives its draft
    # can no longer write (its draft row is gone) and is retired at its next write or open.
    for document in bundle.values():
        try:
            await crdt.close(
                document.record_id, discard=True, draft_epoch=document.snapshot.draft_epoch
            )
        except Exception as error:
            logger.warning(
                "room_not_closed_after_publish",
                closed_record_id=str(document.record_id),
                error=repr(error),
            )
    stubs = sorted(other for other in bundle if other != record_id)
    logger.info("record_published", version=version, stubs=[str(stub) for stub in stubs])
    return PublishResult(version=version, published=[record_id, *stubs], draft_epoch=draft_epoch)


async def discard(
    record_id: UUID,
    user: CurrentUser,
    *,
    sessions: async_sessionmaker[AsyncSession],
    crdt: CrdtWsClient,
    draft_epoch: int | None = None,
) -> DiscardResult:
    """Throw the record's draft away; delete the record itself if it was never published.

    ``draft_epoch`` is the epoch of the draft the caller means (what the editor is connected
    with). Give it whenever the request comes from a person looking at a draft: if the record
    has moved on to another epoch (that draft was published or discarded), the answer is 409
    ``LAE-CRDT-007`` and nothing is touched. Without it, the draft meant is the one the record
    has when the call starts.

    Either way ``crdt-ws`` is asked to discard the draft of exactly that epoch
    (``close(discard=True, draft_epoch=...)``: sockets get 4409, the rows go,
    ``records.draft_epoch`` moves on; ADR-0027), so a draft opened after a publish that
    overtook this call survives it. Such a publish is reported as 409 ``LAE-CRDT-007`` too:
    the draft was not discarded, it was published. A record at ``version = 0`` exists only as
    its draft, so its row is deleted afterwards, unless somebody has opened a new draft of it
    in the meantime. Discarding a record that has no draft is not an error.

    Raises 403 ``LAE-AUTH-001`` without ``publish``, 404 ``LAE-REC-001``, 409 ``LAE-CRDT-007``
    and ``lae.crdt.client.CrdtWsError`` (nothing is deleted while ``crdt-ws`` cannot be
    reached).
    """
    _require_publish(user)
    bind_log_context(user_id=user.id, record_id=record_id)
    found = await _version_and_epoch(sessions, record_id)
    if found is None:
        raise _record_not_found()
    version, current_epoch = found
    # A live draft has the epoch of its record (``lae.crdt.store``), so this is also "the
    # draft the caller named is the one there is, if there is one".
    if draft_epoch is not None and draft_epoch != current_epoch:
        raise draft_superseded(record_id)
    await crdt.close(record_id, discard=True, draft_epoch=current_epoch)
    # The same delete as the draft reaper's: the record row is locked FOR UPDATE, which keeps
    # crdt-ws from creating a draft (it locks the row FOR SHARE first) between the check for a
    # draft and the delete, which would cascade to that draft.
    if await delete_unpublished_record(sessions, record_id):
        logger.info("unpublished_record_deleted")
        return DiscardResult(id=record_id, deleted=True, draft_epoch=None)
    after = await _version_and_epoch(sessions, record_id)
    if after is None:  # deleted meanwhile (by the reaper or another discard)
        return DiscardResult(id=record_id, deleted=True, draft_epoch=None)
    if after[0] != version:
        # A publish overtook the close: the draft became a version instead of being discarded,
        # and the close, limited to its epoch, left whatever draft has been opened since.
        logger.info("discard_overtaken_by_publish", version=after[0])
        raise draft_superseded(record_id)
    logger.info("draft_discarded", draft_epoch=after[1])
    return DiscardResult(id=record_id, deleted=False, draft_epoch=after[1])
