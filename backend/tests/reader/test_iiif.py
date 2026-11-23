"""Finding the IIIF resource Canvas Panel shows (``lae.reader.iiif``)."""

from __future__ import annotations

from typing import Any

import pytest

from lae.corpus import CORPUS_DIR, load_record
from lae.reader.iiif import IiifResource, fallback_image, find_iiif, image_service

SERVICE = "https://iiif.test/image/kylix"
MANIFEST = "https://iiif.test/manifest.json"
PRESENTATION = {"id": "http://iiif.io/api/presentation/3/context.json", "type": "InformationObject"}
IMAGE_API = {"id": "http://iiif.io/api/image", "type": "InformationObject"}


def shown_by(*digital_objects: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "HumanMadeObject",
        "representation": [{"type": "VisualItem", "digitally_shown_by": list(digital_objects)}],
    }


def digital_object(url: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "DigitalObject",
        "access_point": [{"id": url, "type": "DigitalObject"}],
        **extra,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{SERVICE}/full/max/0/default.jpg", (SERVICE, None)),
        (f"{SERVICE}/full/!480,480/0/default.jpg", (SERVICE, None)),
        (f"{SERVICE}/square/256,/90/gray.png", (SERVICE, None)),
        (f"{SERVICE}/10,20,300,200/max/0/default.jpg", (SERVICE, "10,20,300,200")),
        (f"{SERVICE}/10.6,20.4,300.5,200/max/0/default.jpg", (SERVICE, "11,20,300,200")),
        (f"{SERVICE}/pct:10,20,50.5,50/full/0/default.jpg", (SERVICE, "pct:10,20,50.5,50")),
        (f"{SERVICE}/pct:10,20,150,50/full/0/default.jpg", (SERVICE, "pct:10,20,100,50")),
        (f"{SERVICE}/10,20,0,200/max/0/default.jpg", (SERVICE, None)),
        (f"{SERVICE}/info.json", (SERVICE, None)),
        ("https://example.org/images/kylix.jpg", None),
        ("https://example.org/thumbnail/yuag/obj/176060", None),
    ],
)
def test_image_service(url: str, expected: tuple[str, str | None] | None) -> None:
    assert image_service(url) == expected


def test_image_api_url_in_representation() -> None:
    record = shown_by(digital_object(f"{SERVICE}/10,20,300,200/max/0/default.jpg"))
    assert find_iiif(record) == IiifResource(image_service=SERVICE, region="10,20,300,200")
    assert fallback_image(record) == f"{SERVICE}/full/!800,800/0/default.jpg"


def test_image_service_named_by_a_digital_service() -> None:
    record = shown_by(
        digital_object(
            "https://cdn.test/kylix.jpg",
            digitally_available_via=[
                {
                    "type": "DigitalService",
                    "access_point": [{"id": f"{SERVICE}/info.json", "type": "DigitalObject"}],
                    "conforms_to": [IMAGE_API],
                }
            ],
        )
    )
    assert find_iiif(record) == IiifResource(image_service=SERVICE)
    assert fallback_image(record) == "https://cdn.test/kylix.jpg"


def test_digital_object_conforming_to_the_image_api() -> None:
    record = shown_by(digital_object(SERVICE, conforms_to=[IMAGE_API]))
    assert find_iiif(record) == IiifResource(image_service=SERVICE)


def test_manifest_in_representation() -> None:
    record = shown_by(digital_object(MANIFEST, conforms_to=[PRESENTATION]))
    assert find_iiif(record) == IiifResource(manifest=MANIFEST)
    assert fallback_image(record) is None


def test_manifest_by_media_type() -> None:
    media_type = 'application/ld+json;profile="http://iiif.io/api/presentation/3/context.json"'
    record = shown_by(digital_object(MANIFEST, format=media_type))
    assert find_iiif(record) == IiifResource(manifest=MANIFEST)


def test_plain_image_is_not_iiif() -> None:
    record = shown_by(digital_object("https://example.org/kylix.jpg", format="image/jpeg"))
    assert find_iiif(record) is None
    assert fallback_image(record) == "https://example.org/kylix.jpg"


def test_only_http_urls() -> None:
    record = shown_by(digital_object("javascript:alert(1)", conforms_to=[PRESENTATION]))
    assert find_iiif(record) is None
    assert fallback_image(record) is None


def test_representation_comes_before_subject_of() -> None:
    record = shown_by(digital_object(f"{SERVICE}/full/max/0/default.jpg"))
    record["subject_of"] = [
        {
            "type": "LinguisticObject",
            "digitally_carried_by": [digital_object(MANIFEST, conforms_to=[PRESENTATION])],
        }
    ]
    assert find_iiif(record) == IiifResource(image_service=SERVICE)
    del record["representation"]
    assert find_iiif(record) == IiifResource(manifest=MANIFEST)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "linkedart/object-spring-11.json",
            IiifResource(
                image_service="https://media.getty.edu/iiif/image/8094f61e-e458-42bd-90cf-a0ed0dcc90b9"
            ),
        ),
        (
            "linkedart/object-spring-10.json",
            IiifResource(
                manifest="https://media.getty.edu/iiif/manifest/db379bba-801c-4650-bc31-3ff2f712eb21"
            ),
        ),
        (
            "institutional/yuag-obj-31619.json",
            IiifResource(manifest="https://manifests.collections.yale.edu/yuag/obj/31619"),
        ),
        (
            "institutional/yul-node-14a3551f-6e93-4501-9b18-1b107f0274aa.json",
            IiifResource(image_service="https://collections.library.yale.edu/iiif/2/1010619"),
        ),
        ("linkedart/person-rembrandt-15.json", None),
    ],
)
def test_corpus_records(name: str, expected: IiifResource | None) -> None:
    assert find_iiif(load_record(CORPUS_DIR / name)) == expected
