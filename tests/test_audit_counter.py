"""Failed-attempt counter: a flood never reaches the chain."""

import pytest

from dmf_init.audit import AuditLog, FailedAttempts, Retention, Status, verify
from dmf_init.audit.segment import read_entries


def test_flood_keeps_every_chain_entry_and_the_ring_at_1000(tmp_path):
    log = AuditLog(tmp_path / "chain", Retention(segment_bytes=600, max_bytes=10**9))
    for n in range(5):
        log.require_written({"event": "checkin", "actor": "op", "request_id": f"r{n}"})
    before = list(read_entries(tmp_path / "chain"))

    counter = FailedAttempts(tmp_path / "counter")
    for n in range(100_000):
        assert counter.record(f"192.0.2.{n % 250}", "bad assertion")

    assert counter.total == 100_000
    assert len(counter.recent()) == 1000
    assert list(read_entries(tmp_path / "chain")) == before
    assert verify(tmp_path / "chain").status is Status.OK
    counter.close()


def test_totals_survive_reopen(tmp_path):
    counter = FailedAttempts(tmp_path, ring=10)
    for n in range(35):
        counter.record("192.0.2.1", f"r{n}")
    counter.close()
    again = FailedAttempts(tmp_path, ring=10)
    assert again.total == 35
    assert [a["reason"] for a in again.recent()] == [f"r{n}" for n in range(25, 35)]
    again.close()


def test_second_owner_is_refused(tmp_path):
    first = FailedAttempts(tmp_path)
    with pytest.raises(BlockingIOError):
        FailedAttempts(tmp_path)
    first.close()
