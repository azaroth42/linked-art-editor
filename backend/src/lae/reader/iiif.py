"""IIIF resources a record points at, for the Public Reader's Canvas Panel (Step 1.21).

Canvas Panel 1.0.74 is the only viewer (decision D4, ADR-0012); the rules for embedding it are
section 8 of ``docs/spikes/S5-canvas-panel.md``. This module only finds what to show; the page
(``templates/record.html``) and the reader script (``frontend/src/reader``) do the rest.

Where Linked Art puts IIIF (``linked.art`` model, "Digital Integration"):

- ``representation`` → ``VisualItem`` → ``digitally_shown_by`` → ``DigitalObject``, an image whose
  ``access_point`` is the image file. It is a IIIF resource when the ``DigitalObject`` (or a
  ``DigitalService`` it is ``digitally_available_via``) ``conforms_to`` the IIIF Image API, or
  when the access point is a IIIF Image API URL
  (``{service}/{region}/{size}/{rotation}/{quality}.{format}``) or an ``info.json``. A
  ``DigitalObject`` there that conforms to the Presentation API is a Manifest.
- ``subject_of`` → ``LinguisticObject`` → ``digitally_carried_by`` → ``DigitalObject`` that
  ``conforms_to`` the IIIF Presentation API (or has its media type): a Manifest. Real records
  (Yale, Getty) put their Manifests here, so the reader looks here after ``representation``.

The first IIIF resource found, in that order, is shown. An image service comes with the region
of its access point URL, if that names one (``x,y,w,h`` in pixels, rounded and clamped to
non-negative integers, or ``pct:x,y,w,h``); the script applies it as a Content State once the
viewer is ready. Only ``http`` and ``https`` URLs are used. ``fallback_image`` is the first
image the record shows, for the page without JavaScript; from an image service it asks for at
most 800 by 800 pixels rather than the full size the access point may name.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

IMAGE_API: Final = re.compile(r"https?://iiif\.io/api/image(?:/|$)")
PRESENTATION_API: Final = re.compile(r"https?://iiif\.io/api/presentation(?:/|$)")

IMAGE_URL: Final = re.compile(
    r"(?P<service>https?://[^?#]+?)"
    r"/(?P<region>full|square|\d+(?:\.\d+)?(?:,\d+(?:\.\d+)?){3}|pct:[\d.]+(?:,[\d.]+){3})"
    r"/(?P<size>[^/?#]+)/(?P<rotation>!?\d+(?:\.\d+)?)/(?P<quality>[a-z]+)\.(?P<format>[a-z0-9]+)"
)
"""A IIIF Image API 2 or 3 image request URL."""

INFO_JSON: Final = "/info.json"
FALLBACK_SIZE: Final = "!800,800"
"""Size of the image requested from a bare image service for the page without JavaScript."""


@dataclass(frozen=True)
class IiifResource:
    """What Canvas Panel is to show: a Manifest, or an image service (with a region)."""

    manifest: str | None = None
    image_service: str | None = None
    region: str | None = None


def as_list(value: Any) -> list[Any]:
    """JSON-LD allows one object where an array is usual; treat it as a one-item array."""
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _objects(value: Any) -> Iterator[Mapping[str, Any]]:
    for item in as_list(value):
        if isinstance(item, Mapping):
            yield item


def http_url(value: Any) -> str | None:
    """Return ``value`` if it is an ``http`` or ``https`` URL, else ``None``."""
    if isinstance(value, str) and re.match(r"https?://[^\s]+$", value):
        return value
    return None


def access_points(digital_object: Mapping[str, Any]) -> list[str]:
    """Return the ``http(s)`` URLs of a ``DigitalObject``'s ``access_point`` entries."""
    urls = [http_url(point.get("id")) for point in _objects(digital_object.get("access_point"))]
    return [url for url in urls if url is not None]


def conforms_to(node: Mapping[str, Any], api: re.Pattern[str]) -> bool:
    """True if ``node`` ``conforms_to`` ``api`` or its ``format`` names that API's profile."""
    for standard in _objects(node.get("conforms_to")):
        if isinstance(standard.get("id"), str) and api.match(standard["id"]):
            return True
    media_type = node.get("format")
    return isinstance(media_type, str) and api.search(media_type) is not None


def _clean_region(region: str) -> str | None:
    """Return a region as non-negative integers (pixels) or ``pct:`` numbers; ``None`` for all."""
    if region in ("full", "square"):
        return None
    percent = region.startswith("pct:")
    numbers = [float(part) for part in region.removeprefix("pct:").split(",")]
    if percent:
        x, y, w, h = (min(max(n, 0.0), 100.0) for n in numbers)
        if w <= 0 or h <= 0:
            return None
        return "pct:" + ",".join(f"{n:g}" for n in (x, y, w, h))
    x, y, w, h = (max(round(n), 0) for n in numbers)
    if w <= 0 or h <= 0:
        return None
    return f"{x},{y},{w},{h}"


def image_service(url: str) -> tuple[str, str | None] | None:
    """Return ``(service, region)`` for a IIIF Image API URL or ``info.json``, else ``None``."""
    if url.endswith(INFO_JSON):
        return url.removesuffix(INFO_JSON), None
    match = IMAGE_URL.fullmatch(url)
    if match is None:
        return None
    return match["service"], _clean_region(match["region"])


def _service_base(url: str) -> str:
    return url.removesuffix(INFO_JSON).rstrip("/")


def from_digital_object(digital_object: Mapping[str, Any]) -> IiifResource | None:
    """Return the IIIF resource an image ``DigitalObject`` points at, if any."""
    urls = access_points(digital_object)
    if conforms_to(digital_object, PRESENTATION_API) and urls:
        return IiifResource(manifest=urls[0])
    parsed = [found for url in urls if (found := image_service(url)) is not None]
    region = next((region for _, region in parsed if region is not None), None)
    for service in _objects(digital_object.get("digitally_available_via")):
        service_urls = access_points(service)
        if conforms_to(service, IMAGE_API) and service_urls:
            return IiifResource(image_service=_service_base(service_urls[0]), region=region)
    if parsed:
        return IiifResource(image_service=parsed[0][0], region=region)
    if conforms_to(digital_object, IMAGE_API) and urls:
        return IiifResource(image_service=_service_base(urls[0]))
    return None


def image_objects(record: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield ``representation`` → ``VisualItem`` → ``digitally_shown_by`` ``DigitalObject``s."""
    for visual_item in _objects(record.get("representation")):
        yield from _objects(visual_item.get("digitally_shown_by"))


def find_iiif(record: Mapping[str, Any]) -> IiifResource | None:
    """Return the first IIIF resource of a record (see the module docstring), else ``None``."""
    for digital_object in image_objects(record):
        found = from_digital_object(digital_object)
        if found is not None:
            return found
    for text in _objects(record.get("subject_of")):
        for digital_object in _objects(text.get("digitally_carried_by")):
            urls = access_points(digital_object)
            if conforms_to(digital_object, PRESENTATION_API) and urls:
                return IiifResource(manifest=urls[0])
    return None


def fallback_image(record: Mapping[str, Any]) -> str | None:
    """Return the URL of the first image the record shows, for the page without JavaScript."""
    for digital_object in image_objects(record):
        if conforms_to(digital_object, PRESENTATION_API):
            continue
        for url in access_points(digital_object):
            parsed = image_service(url)
            return url if parsed is None else _image_url(parsed[0])
        for service in _objects(digital_object.get("digitally_available_via")):
            service_urls = access_points(service)
            if conforms_to(service, IMAGE_API) and service_urls:
                return _image_url(service_urls[0])
    return None


def _image_url(service: str) -> str:
    """An image of at most 800 by 800 pixels from an image service (Image API 2 and 3)."""
    return f"{_service_base(service)}/full/{FALLBACK_SIZE}/0/default.jpg"
