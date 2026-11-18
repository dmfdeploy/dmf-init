"""Queries the revoker uses: open intents and recorded accessors."""

from dmf_init.audit import AuditLog, open_intents, recorded_accessors

ACC_A, ACC_B = "AccessorAAAAAAAAAAAAAAAA", "AccessorBBBBBBBBBBBBBBBB"


def intent(log, request_id):
    log.require_written({"event": "intent", "actor": "op", "request_id": request_id})


def test_intent_without_closed_is_open(tmp_path):
    log = AuditLog(tmp_path)
    intent(log, "q1")
    assert [e["request_id"] for e in open_intents(tmp_path)] == ["q1"]


def test_closed_intent_is_not_open(tmp_path):
    log = AuditLog(tmp_path)
    intent(log, "q1")
    intent(log, "q2")
    log.require_written({"event": "closed", "request_id": "q1", "outcome": "revoked"})
    assert [e["request_id"] for e in open_intents(tmp_path)] == ["q2"]


def test_minted_accessors_are_recorded_even_after_close(tmp_path):
    log = AuditLog(tmp_path)
    intent(log, "q1")
    log.require_written({"event": "minted", "request_id": "q1", "accessors": [ACC_A, ACC_B]})
    assert recorded_accessors(tmp_path) == [ACC_A, ACC_B]
    log.require_written({"event": "closed", "request_id": "q1", "outcome": "revoked"})
    assert recorded_accessors(tmp_path) == [ACC_A, ACC_B]
    assert open_intents(tmp_path) == []


def test_empty_directory_has_nothing_open(tmp_path):
    assert open_intents(tmp_path / "missing") == []
    assert recorded_accessors(tmp_path / "missing") == []
