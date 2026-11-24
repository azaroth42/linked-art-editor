"""What the Public Reader shows of a record: a view model for ``templates/record.html``.

``build_page`` reads the public document (stripped of internal node IRIs, labels transcluded:
``lae.api.public.public_document``) and returns plain data. Every word of chrome comes from the
reader catalogue (``lae.reader.i18n``); every other string is the record's own, with the
language it is known to be in (a ``Name``'s or ``LinguisticObject``'s ``language``, or the
language of a transcluded label), so the template can mark it with ``lang``.

Sections, in order, each only when the record has something for it: names, identifiers,
classifications, one per event (production, creation, birth, death, formation, dissolution,
destruction: who, when, where, technique), materials, dimensions, statements (``referred_to_by``
texts), and related records (every other top-level property whose values are references). A
reference is a link: to ``/id/<uuid>`` for a record of this installation, else to its URI when
that is ``http(s)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from babel.dates import format_skeleton
from babel.numbers import format_decimal

from lae.ids import record_id_from_uri, record_uri
from lae.labels.extract import labels_from_record
from lae.labels.langtags import UNDETERMINED, language_tag
from lae.labels.transclude import label_language, resolve_label
from lae.reader.dates import describe
from lae.reader.i18n import Catalogue, Direction
from lae.reader.iiif import IiifResource, as_list, fallback_image, find_iiif, http_url

LABEL: Final = "_label"
PUBLIC_PATH: Final = "/id/"
"""Path of the public dereference routes (``lae.api.public``)."""

TURTLE_TYPE: Final = "text/turtle"

EDIT_PATH: Final = "/edit/"
"""Path of the editor route in the SPA (``frontend/src/app/routes.tsx``)."""

EVENTS: Final = (
    "produced_by",
    "created_by",
    "born",
    "died",
    "formed_by",
    "dissolved_by",
    "destroyed_by",
)
"""Top-level properties holding an event, shown as a section each, in this order."""

HANDLED: Final = frozenset(
    {
        "@context",
        "id",
        "type",
        LABEL,
        "identified_by",
        "classified_as",
        "made_of",
        "dimension",
        "referred_to_by",
        "representation",
        "subject_of",
        *EVENTS,
    }
)
"""Top-level properties with a section (or none) of their own; the rest are "related"."""


@dataclass(frozen=True)
class Value:
    """One value: text in a language (``None``: the page's), maybe a link, maybe a note."""

    text: str
    lang: str | None = None
    href: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Entry:
    """A term and its values (``<dt>`` and ``<dd>``s)."""

    term: str
    values: tuple[Value, ...]
    term_lang: str | None = None


@dataclass(frozen=True)
class Section:
    """A titled section: a list of terms with values, or a plain list of values."""

    slug: str
    heading: str
    entries: tuple[Entry, ...] = ()
    values: tuple[Value, ...] = ()


@dataclass(frozen=True)
class Media:
    """The record's image: Canvas Panel's input, and an image for the page without script."""

    iiif: IiifResource | None
    image: str | None
    alt: str
    manifest_link: str | None


@dataclass(frozen=True)
class Banner:
    """The notice shown on ``?version=N``."""

    text: str
    current_href: str | None
    current_text: str | None


@dataclass(frozen=True)
class RecordPage:
    """Everything ``templates/record.html`` renders."""

    lang: str
    dir: Direction
    title: Value
    page_title: str
    class_label: str | None
    sections: tuple[Section, ...]
    media: Media | None
    banner: Banner | None
    data_links: tuple[Value, ...]
    edit_href: str | None
    script_src: str | None
    alternates: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PageVersion:
    """Which stored version the page shows."""

    requested: int | None
    current: int
    published: datetime


class _Builder:
    """Turns one public document into sections; holds what every value needs."""

    def __init__(
        self,
        catalogue: Catalogue,
        base_uri: str,
        label_languages: Mapping[str, str],
    ) -> None:
        self.catalogue = catalogue
        self.base_uri = base_uri
        self.label_languages = label_languages

    # --- values -------------------------------------------------------------------------------

    def link(self, uri: str) -> str | None:
        """``/id/<uuid>`` for a record of this installation, the URI if ``http(s)``, else None."""
        record_id = record_id_from_uri(uri, base_uri=self.base_uri)
        if record_id is not None:
            return PUBLIC_PATH + str(record_id)
        return http_url(uri)

    def reference(self, node: Mapping[str, Any], note: str | None = None) -> Value | None:
        """The value of a reference: its label (or URI) linked to its target."""
        uri = node.get("id")
        if not isinstance(uri, str):
            return None
        label = node.get(LABEL)
        record_id = record_id_from_uri(uri, base_uri=self.base_uri)
        if isinstance(label, str) and label.strip():
            text = label
        elif record_id is not None:
            text = record_uri(record_id, base_uri=self.base_uri)
        else:
            text = uri
        return Value(text, self.label_languages.get(uri), self.link(uri), note)

    def references(self, value: Any) -> tuple[Value, ...]:
        """The references among the items of a property."""
        found = (self.reference(item) for item in as_list(value) if isinstance(item, Mapping))
        return tuple(item for item in found if item is not None)

    def first_label(self, node: Mapping[str, Any], fallback_key: str) -> tuple[str, str | None]:
        """The label of a node's first classification, else the catalogue's ``fallback_key``."""
        for value in self.references(node.get("classified_as")):
            return value.text, value.lang
        return self.catalogue.t(fallback_key), None

    def text(self, node: Mapping[str, Any]) -> Value | None:
        """The ``content`` of a ``Name``, ``Identifier`` or ``LinguisticObject``, with language."""
        content = node.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        languages = as_list(node.get("language"))
        tag = language_tag(languages[0]) if languages else UNDETERMINED
        return Value(content, None if tag == UNDETERMINED else tag)

    # --- sections -----------------------------------------------------------------------------

    def grouped(self, items: Iterable[tuple[tuple[str, str | None], Value]]) -> tuple[Entry, ...]:
        """Entries with one term per distinct label, in order of first appearance."""
        order: list[tuple[str, str | None]] = []
        values: dict[tuple[str, str | None], list[Value]] = {}
        for term, value in items:
            if term not in values:
                order.append(term)
                values[term] = []
            values[term].append(value)
        return tuple(Entry(text, tuple(values[(text, lang)]), lang) for text, lang in order)

    def contents(self, record: Mapping[str, Any], kind: str, fallback: str) -> tuple[Entry, ...]:
        """Entries for the ``identified_by`` items of one ``type``, by classification."""

        def items() -> Iterator[tuple[tuple[str, str | None], Value]]:
            for node in as_list(record.get("identified_by")):
                if isinstance(node, Mapping) and node.get("type") == kind:
                    value = self.text(node)
                    if value is not None:
                        if kind == "Identifier":
                            value = Value(value.text)
                        yield self.first_label(node, fallback), value

        return self.grouped(items())

    def date(self, timespan: Mapping[str, Any]) -> Value | None:
        """A ``TimeSpan``: its display name (if any) and the period its bounds describe."""
        names = [
            value
            for node in as_list(timespan.get("identified_by"))
            if isinstance(node, Mapping) and (value := self.text(node)) is not None
        ]
        period = describe(timespan, self.catalogue.locale)
        period_text = None
        if period is not None:
            period_text = (
                period.text
                if period.kind == "exact"
                else self.catalogue.t(f"date.{period.kind}", date=period.text)
            )
        if names:
            name = names[0]
            note = None
            if period_text is not None and period_text != name.text:
                note = self.catalogue.t("value.note", note=period_text)
            return Value(name.text, name.lang, note=note)
        return None if period_text is None else Value(period_text)

    def event(self, key: str, index: int, node: Mapping[str, Any]) -> Section | None:
        """A section for one event: who, when, where, technique."""
        t = self.catalogue.t
        agents = list(self.references(node.get("carried_out_by")))
        techniques = list(self.references(node.get("technique")))
        for part in as_list(node.get("part")):
            if not isinstance(part, Mapping):
                continue
            roles = self.references(part.get("classified_as"))
            note = t("value.note", note=roles[0].text) if roles else None
            for agent in as_list(part.get("carried_out_by")):
                if isinstance(agent, Mapping) and (value := self.reference(agent, note)):
                    agents.append(value)
            techniques.extend(self.references(part.get("technique")))
        entries: list[Entry] = []
        if agents:
            entries.append(Entry(t("field.carried_out_by"), tuple(agents)))
        timespan = node.get("timespan")
        if isinstance(timespan, Mapping) and (date := self.date(timespan)) is not None:
            entries.append(Entry(t("field.timespan"), (date,)))
        if places := self.references(node.get("took_place_at")):
            entries.append(Entry(t("field.took_place_at"), places))
        if techniques:
            entries.append(Entry(t("field.technique"), tuple(techniques)))
        if not entries:
            return None
        slug = key if index == 0 else f"{key}-{index + 1}"
        return Section(slug, t(f"event.{key}"), tuple(entries))

    def dimensions(self, record: Mapping[str, Any]) -> tuple[Entry, ...]:
        """Entries for ``dimension``: the number in the page's locale and its unit's label."""
        t = self.catalogue.t

        def items() -> Iterator[tuple[tuple[str, str | None], Value]]:
            for node in as_list(record.get("dimension")):
                if not isinstance(node, Mapping):
                    continue
                number = node.get("value")
                if isinstance(number, bool) or not isinstance(number, int | float):
                    continue
                shown = format_decimal(
                    number, locale=self.catalogue.locale, decimal_quantization=False
                )
                unit = node.get("unit")
                unit_value = self.reference(unit) if isinstance(unit, Mapping) else None
                text = (
                    t("dimension.value", value=shown, unit=unit_value.text) if unit_value else shown
                )
                yield self.first_label(node, "dimension.untyped"), Value(text)

        return self.grouped(items())

    def statements(self, record: Mapping[str, Any]) -> tuple[Entry, ...]:
        """Entries for the texts in ``referred_to_by``, by classification."""

        def items() -> Iterator[tuple[tuple[str, str | None], Value]]:
            for node in as_list(record.get("referred_to_by")):
                if isinstance(node, Mapping) and (value := self.text(node)) is not None:
                    yield self.first_label(node, "statement.untyped"), value

        return self.grouped(items())

    def related(self, record: Mapping[str, Any]) -> tuple[Entry, ...]:
        """Entries for every other top-level property with references, and cited documents."""
        entries: list[Entry] = []
        cited = tuple(
            value
            for node in as_list(record.get("referred_to_by"))
            if isinstance(node, Mapping)
            and self.text(node) is None
            and (value := self.reference(node)) is not None
        )
        candidates: list[tuple[str, tuple[Value, ...]]] = [("referred_to_by", cited)]
        candidates += [
            (key, self.references(value)) for key, value in record.items() if key not in HANDLED
        ]
        for key, values in candidates:
            if values:
                term_key = f"property.{key}"
                term = self.catalogue.t(term_key) if self.catalogue.has(term_key) else key
                entries.append(Entry(term, values))
        return tuple(entries)

    def sections(self, record: Mapping[str, Any]) -> tuple[Section, ...]:
        """Every section the record has content for, in page order."""
        t = self.catalogue.t
        found: list[Section] = []
        if names := self.contents(record, "Name", "name.untyped"):
            found.append(Section("names", t("section.names"), names))
        if identifiers := self.contents(record, "Identifier", "identifier.untyped"):
            found.append(Section("identifiers", t("section.identifiers"), identifiers))
        if classes := self.references(record.get("classified_as")):
            found.append(Section("classifications", t("section.classifications"), values=classes))
        for key in EVENTS:
            for index, node in enumerate(as_list(record.get(key))):
                if isinstance(node, Mapping) and (section := self.event(key, index, node)):
                    found.append(section)
        if materials := self.references(record.get("made_of")):
            found.append(Section("materials", t("section.materials"), values=materials))
        if dimensions := self.dimensions(record):
            found.append(Section("dimensions", t("section.dimensions"), dimensions))
        if statements := self.statements(record):
            found.append(Section("statements", t("section.statements"), statements))
        if related := self.related(record):
            found.append(Section("related", t("section.related"), related))
        return tuple(found)


def record_title(
    record: Mapping[str, Any],
    chain: Sequence[str],
    catalogue: Catalogue,
    preferred_language: str,
) -> Value:
    """The record's label in the caller's locale chain (``entity_labels`` rules), with language.

    ``preferred_language`` is ``Settings.label_language``: the default label prefers a primary
    name in it, as the record's ``entity_labels`` row does (Step 1.19a).
    """
    labels, default_label = labels_from_record(record, preferred_language)
    if not labels and not (isinstance(record.get(LABEL), str) and record[LABEL].strip()):
        return Value(catalogue.t("record.untitled"))
    label = resolve_label(labels, default_label, chain)
    return Value(label, label_language(labels, label))


def _version_query(version: int | None) -> str:
    return "" if version is None else f"version={version}&"


def build_page(
    record: Mapping[str, Any],
    *,
    record_id: UUID,
    chain: Sequence[str],
    catalogue: Catalogue,
    base_uri: str,
    label_languages: Mapping[str, str],
    preferred_language: str,
    version: PageVersion,
    signed_in: bool,
    script_src: str,
    jsonld_type: str,
) -> RecordPage:
    """Return the view model of the Public Reader page of ``record`` (see the module docstring).

    ``preferred_language`` is ``Settings.label_language`` (``record_title``).
    """
    t = catalogue.t
    locale = catalogue.locale
    builder = _Builder(catalogue, base_uri, label_languages)
    title = record_title(record, chain, catalogue, preferred_language)
    kind = record.get("type")
    class_label = None
    if isinstance(kind, str):
        class_label = t(f"class.{kind}") if catalogue.has(f"class.{kind}") else kind

    iiif = find_iiif(record)
    image = fallback_image(record)
    media = None
    if iiif is not None or image is not None:
        manifest_link = iiif.manifest if iiif is not None and image is None else None
        media = Media(iiif, image, t("media.alt", title=title.text), manifest_link)

    banner = None
    if version.requested is not None:
        number = format_decimal(version.requested, locale=locale)
        published = format_skeleton("yMMMd", version.published.date(), locale=locale)
        text = t("version.banner", version=number, date=published)
        if version.requested == version.current:
            banner = Banner(text, None, t("version.is_current"))
        else:
            banner = Banner(text, PUBLIC_PATH + str(record_id), t("version.view_current"))

    query = _version_query(version.requested)
    jsonld_href, turtle_href = f"?{query}format=jsonld", f"?{query}format=ttl"
    return RecordPage(
        lang=catalogue.language,
        dir=catalogue.direction,
        title=title,
        page_title=t("site.page_title", title=title.text),
        class_label=class_label,
        sections=builder.sections(record),
        media=media,
        banner=banner,
        data_links=(
            Value(t("data.jsonld"), href=jsonld_href),
            Value(t("data.turtle"), href=turtle_href),
        ),
        edit_href=EDIT_PATH + str(record_id) if signed_in else None,
        script_src=script_src if iiif is not None else None,
        alternates=((jsonld_type, jsonld_href), (TURTLE_TYPE, turtle_href)),
    )
