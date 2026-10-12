import json
from pathlib import Path

import pytest

from mail_actions.ids import InvalidRef, MessageRef, UnknownKind, parse_ref

# Synthetic vectors maintained here against the corpus.id/v1 grammar
# (see docs/message-ids.md).
VECTORS = json.loads((Path(__file__).parent / "vectors" / "message_ids.json").read_text())


def test_vectors_pin_the_contract():
    assert VECTORS["version"] == "corpus.id/v1"


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["id"])
def test_valid_vectors_parse_and_round_trip(case):
    ref = parse_ref(case["id"])

    assert ref == MessageRef(case["kind"], case["mailbox"], case["uid"])
    assert str(ref) == case["id"]
    assert ref.source == f"{case['kind']}:{case['mailbox']}"
    if "imap" in case:
        parts = case["imap"]
        assert ref.imap_parts() == (parts["folder"], parts["uidvalidity"], parts["uid"])


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["why"])
def test_invalid_vectors_are_refused(case):
    with pytest.raises(InvalidRef):
        parse_ref(case["id"])


@pytest.mark.parametrize("value", ["pop3:home::0a1b2c3d", "outlook_2:home::anything"])
def test_well_formed_unknown_kind_is_its_own_error(value):
    with pytest.raises(UnknownKind):
        parse_ref(value)


@pytest.mark.parametrize(
    "value",
    ["Gmail:home::0a1b2c3d", "gmail.x:home::0a1b2c3d", "gmail:home:0a1b2c3d", "no colons", ""],
)
def test_malformed_input_is_not_an_unknown_kind(value):
    with pytest.raises(InvalidRef) as exc:
        parse_ref(value)
    assert not isinstance(exc.value, UnknownKind)


@pytest.mark.parametrize("value", [None, 42, "gmail:personal::" + "a" * 2000])
def test_non_strings_and_oversized_input_are_refused(value):
    with pytest.raises(InvalidRef):
        parse_ref(value)


def test_imap_parts_only_for_imap_refs():
    with pytest.raises(ValueError):
        parse_ref("gmail:personal::18c2f4a9b0d1e7f3").imap_parts()
