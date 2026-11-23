"""``lae.crdt.protocol``: awareness codec, update validation, integration check."""

from __future__ import annotations

from typing import Any

import pytest
from pycrdt import Awareness, Doc, Text

from lae.crdt.protocol import (
    EMPTY_UPDATE,
    ProtocolError,
    check_update,
    decode_awareness,
    decode_state_vector,
    encode_awareness,
    frame_payload,
    is_integrated,
)


def text_updates() -> list[bytes]:
    """Updates typing ``abc`` one letter at a time, then deleting the ``b``."""
    doc: Doc[Any] = Doc()
    text = doc.get("t", type=Text)
    updates: list[bytes] = []
    doc.observe(lambda event: updates.append(event.update))
    text += "a"
    text += "b"
    text += "c"
    del text[1]
    return updates


def text_of(doc: Doc[Any]) -> str:
    return str(doc.get("t", type=Text))


def test_awareness_codec_matches_pycrdt() -> None:
    doc: Doc[Any] = Doc()
    awareness = Awareness(doc)
    awareness.set_local_state({"user": {"name": "Ada ✓"}, "n": 1})
    encoded = awareness.encode_awareness_update([doc.client_id])
    entries = decode_awareness(encoded)
    assert entries == [(doc.client_id, 1, {"user": {"name": "Ada ✓"}, "n": 1})]
    other = Awareness(Doc())
    other.apply_awareness_update(encode_awareness(entries), "test")
    assert other.states[doc.client_id] == {"user": {"name": "Ada ✓"}, "n": 1}


def test_awareness_null_and_non_object_states_decode_as_leaving() -> None:
    payload = encode_awareness([(7, 3, None)])
    assert decode_awareness(payload) == [(7, 3, None)]
    not_an_object = b"\x01\x07\x03\x02[]"
    assert decode_awareness(not_an_object) == [(7, 3, None)]


@pytest.mark.parametrize(
    "payload", [b"", b"\x02\x01\x01\x02{}", b"\x01\x01\x01\x05{}", b"\x01\x01\x01\x01{"]
)
def test_malformed_awareness_is_rejected(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_awareness(payload)


def test_frame_payload_checks_the_length() -> None:
    assert frame_payload(b"\x03abc") == b"abc"
    with pytest.raises(ProtocolError):
        frame_payload(b"\x05abc")
    with pytest.raises(ProtocolError):
        frame_payload(b"")


@pytest.mark.parametrize("update", [b"", b"\x00", b"\x01", b"not an update"])
def test_check_update_rejects_bytes_that_are_not_an_update(update: bytes) -> None:
    with pytest.raises(ProtocolError):
        check_update(update)


def test_check_update_accepts_real_updates() -> None:
    check_update(EMPTY_UPDATE)
    for update in text_updates():
        check_update(update)


def test_state_vector_decodes_to_clocks() -> None:
    doc: Doc[Any] = Doc()
    doc.get("t", type=Text).insert(0, "abc")
    assert decode_state_vector(doc.get_state()) == {doc.client_id: 3}


def test_is_integrated_for_updates_applied_in_order() -> None:
    updates = text_updates()
    doc: Doc[Any] = Doc()
    for update in updates:
        doc.apply_update(update)
        assert is_integrated(doc, update)
    assert all(is_integrated(doc, update) for update in updates)
    assert text_of(doc) == "ac"


def test_is_integrated_sees_a_struct_waiting_for_its_dependency() -> None:
    insert_a, insert_b, insert_c, _delete_b = text_updates()
    doc: Doc[Any] = Doc()
    doc.apply_update(insert_c)
    assert text_of(doc) == ""
    assert not is_integrated(doc, insert_c)
    doc.apply_update(insert_a)
    assert not is_integrated(doc, insert_c)
    doc.apply_update(insert_b)
    assert is_integrated(doc, insert_c)
    assert text_of(doc) == "abc"


def test_is_integrated_sees_a_delete_waiting_for_its_target() -> None:
    insert_a, insert_b, _insert_c, delete_b = text_updates()
    doc: Doc[Any] = Doc()
    doc.apply_update(insert_a)
    doc.apply_update(delete_b)
    assert not is_integrated(doc, delete_b)
    doc.apply_update(insert_b)
    assert is_integrated(doc, delete_b)
    assert text_of(doc) == "a"


def test_doc_get_update_drops_pending_parts() -> None:
    """The measured pycrdt behaviour that invariant 4 of ``lae.crdt.rooms`` exists for."""
    insert_a, insert_b, _insert_c, delete_b = text_updates()
    doc: Doc[Any] = Doc()
    doc.apply_update(insert_a)
    doc.apply_update(delete_b)  # pending: its target has not arrived
    restored: Doc[Any] = Doc()
    restored.apply_update(doc.get_update())
    restored.apply_update(insert_b)
    assert text_of(restored) == "ab", "pycrdt now keeps pending deletes: revisit Room._compact"
