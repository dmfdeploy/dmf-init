"""Schema: what may and may not enter the record."""

import pytest

from dmf_init.audit import AuditLog, AuditSchemaError
from dmf_init.audit.schema import validate
from dmf_init.audit.segment import read_entries

# Built at runtime so the source holds no key-shaped literal. 22 token characters.
TOKEN_BODY = "Ab1" * 7 + "X"
OTHER_ACCESSOR = "Zy9" * 7 + "Q"


@pytest.mark.parametrize("prefix", ["s.", "b.", "r.", "hvs.", "hvb.", "hvr."])
def test_token_shaped_value_is_rejected(prefix):
    with pytest.raises(AuditSchemaError):
        validate({"event": "change", "detail": f"minted {prefix}{TOKEN_BODY}"})


def test_token_inside_claimed_is_rejected():
    with pytest.raises(AuditSchemaError):
        validate({"event": "unseal", "claimed": {"note": f"s.{TOKEN_BODY}"}})


def test_accessor_is_accepted():
    accessors = [TOKEN_BODY, OTHER_ACCESSOR]
    validate({"event": "minted", "request_id": "q1", "accessors": accessors})


def test_short_dotted_text_is_not_mistaken_for_a_token():
    validate({"event": "change", "detail": "see s.1 and pass.through"})


def test_unknown_event_and_unknown_field_are_rejected():
    with pytest.raises(AuditSchemaError):
        validate({"event": "root_token_minted"})
    with pytest.raises(AuditSchemaError):
        validate({"event": "checkin", "token": "x"})


def test_reserved_fields_cannot_be_supplied():
    with pytest.raises(AuditSchemaError):
        validate({"event": "checkin", "seq": 99})


@pytest.mark.parametrize("text", ["Biometric", "used biometrics"])
def test_biometric_is_rejected_in_values(text):
    with pytest.raises(AuditSchemaError):
        validate({"event": "unseal", "uv": text})


def test_biometric_is_rejected_in_keys():
    with pytest.raises(AuditSchemaError):
        validate({"event": "unseal", "claimed": {"biometric_ok": True}})


def test_claimed_nesting_is_kept(tmp_path):
    log = AuditLog(tmp_path)
    stored = log.require_written(
        {"event": "unseal", "actor": "a", "uv": "verified", "claimed": {"device": "phone"}}
    )
    assert stored["claimed"] == {"device": "phone"}
    assert "device" not in stored
    assert list(read_entries(tmp_path))[-1]["claimed"] == {"device": "phone"}


def test_minted_needs_exactly_two_accessors():
    with pytest.raises(AuditSchemaError):
        validate({"event": "minted", "request_id": "q1", "accessors": ["only-one-acc"]})


def test_restore_needs_a_link_or_unlinked():
    with pytest.raises(AuditSchemaError):
        validate({"event": "restore", "actor": "op"})
    with pytest.raises(AuditSchemaError):
        validate({"event": "restore", "previous_seq": 3})
    with pytest.raises(AuditSchemaError):
        validate({"event": "restore", "previous_seq": 3, "previous_hash": "ab", "unlinked": True})


def test_rejected_entry_writes_nothing(tmp_path):
    log = AuditLog(tmp_path)
    with pytest.raises(AuditSchemaError):
        log.require_written({"event": "change", "detail": f"b.{TOKEN_BODY}"})
    assert not list(tmp_path.glob("segment-*"))


def test_genesis_cannot_be_appended_by_a_caller(tmp_path):
    log = AuditLog(tmp_path)
    log.require_written({"event": "checkin", "actor": "op"})
    with pytest.raises(AuditSchemaError):
        log.require_written({"event": "genesis"})
