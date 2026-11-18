"""Chain: tamper and gap detection, trimming, the write gate, and concurrent writers."""

import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dmf_init.audit import AuditLog, AuditWriteError, Retention, Status, verify
from dmf_init.audit.segment import ANCHOR_NAME, segments

SMALL = Retention(segment_bytes=600, max_bytes=2500)


def checkin(n: int) -> dict:
    return {"event": "checkin", "actor": "operator", "request_id": f"r{n}", "reason": "monthly"}


def filled(path: Path, count: int, policy: Retention | None = None) -> AuditLog:
    log = AuditLog(path, policy)
    for n in range(count):
        log.require_written(checkin(n))
    return log


def rewrite_lines(path: Path, edit) -> None:
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(edit(lines)))


def test_fresh_chain_starts_with_genesis_and_verifies(tmp_path):
    filled(tmp_path, 3)
    result = verify(tmp_path)
    assert result.status is Status.OK
    assert result.entries == 4  # genesis + 3


def test_edited_byte_is_tamper_at_that_seq(tmp_path):
    filled(tmp_path, 6)
    path = segments(tmp_path)[0][1]
    # seq 4 is the third check-in. Changing "r2" to "r9" keeps the JSON valid, so
    # only the hash can catch it.
    rewrite_lines(path, lambda lines: [ln.replace('"r2"', '"r9"') for ln in lines])
    result = verify(tmp_path)
    assert (result.status, result.seq) == (Status.TAMPER, 4)


def test_edited_entry_with_recomputed_hash_breaks_the_next_link(tmp_path):

    from dmf_init.audit.segment import canonical, entry_hash

    filled(tmp_path, 6)
    path = segments(tmp_path)[0][1]

    def forge(lines):
        entry = json.loads(lines[3])
        entry["reason"] = "forged"
        entry["hash"] = entry_hash(entry)
        lines[3] = canonical(entry).decode() + "\n"
        return lines

    rewrite_lines(path, forge)
    result = verify(tmp_path)
    assert (result.status, result.seq) == (Status.TAMPER, 5)


def test_deleted_middle_line_is_a_gap(tmp_path):
    filled(tmp_path, 6)
    path = segments(tmp_path)[0][1]
    rewrite_lines(path, lambda lines: lines[:3] + lines[4:])  # drop seq 4
    result = verify(tmp_path)
    assert (result.status, result.seq) == (Status.GAP, 4)


def test_deleted_middle_segment_is_a_gap(tmp_path):
    filled(tmp_path, 12, Retention(segment_bytes=300))
    found = segments(tmp_path)
    assert len(found) >= 3
    found[1][1].unlink()
    assert verify(tmp_path).status is Status.GAP


def test_deleted_first_segment_without_anchor_is_a_gap_not_trimmed(tmp_path):
    filled(tmp_path, 12, Retention(segment_bytes=300))
    segments(tmp_path)[0][1].unlink()
    assert verify(tmp_path).status is Status.GAP


def test_trimmed_record_verifies_as_trimmed_not_gap(tmp_path):
    filled(tmp_path, 60, SMALL)
    assert (tmp_path / ANCHOR_NAME).exists()
    result = verify(tmp_path)
    assert result.status is Status.TRIMMED
    assert segments(tmp_path)[0][0] > 1


def test_wrong_anchor_hash_is_not_trimmed(tmp_path):
    filled(tmp_path, 60, SMALL)
    anchor = tmp_path / ANCHOR_NAME
    anchor.write_text(anchor.read_text().replace(anchor.read_text()[-6:-2], "0000"))
    assert verify(tmp_path).status is not Status.TRIMMED


def test_full_record_still_accepts_appends_and_keeps_sequence(tmp_path):
    log = filled(tmp_path, 200, SMALL)
    size = sum(p.stat().st_size for _, p in segments(tmp_path))
    assert size <= SMALL.max_bytes + SMALL.segment_bytes
    last = log.require_written(checkin(999))
    assert last["seq"] == 202  # genesis + 200 + this one
    assert verify(tmp_path).intact


def test_old_segments_are_trimmed_by_age_and_the_newest_is_kept(tmp_path):
    clock = {"now": datetime(2025, 1, 1, tzinfo=UTC)}
    log = AuditLog(tmp_path, Retention(segment_bytes=300), lambda: clock["now"])
    for n in range(8):
        log.require_written(checkin(n))
    before = len(segments(tmp_path))
    clock["now"] += timedelta(days=400)
    for n in range(8):
        log.require_written(checkin(n))
    after = segments(tmp_path)
    assert before >= 2
    assert after[0][0] > 1  # the January segments are gone
    assert verify(tmp_path).status is Status.TRIMMED


def test_require_written_raises_and_appends_nothing_when_fsync_fails(tmp_path, monkeypatch):
    log = filled(tmp_path, 2)
    path = segments(tmp_path)[0][1]
    before = path.read_bytes()

    def broken(fd):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", broken)
    with pytest.raises(AuditWriteError):
        log.require_written(checkin(9))
    assert path.read_bytes() == before


def test_require_written_raises_when_write_fails(tmp_path, monkeypatch):
    log = filled(tmp_path, 2)
    path = segments(tmp_path)[0][1]
    before = path.read_bytes()

    def broken(fd, data):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "write", broken)
    with pytest.raises(AuditWriteError):
        log.require_written(checkin(9))
    assert path.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_directory_blocks_the_gate_but_not_the_unseal_path(tmp_path):
    log = filled(tmp_path, 2)
    tmp_path.chmod(0o500)
    try:
        # Rolling needs a new file, so force a roll to hit the permission error.
        log.policy = Retention(segment_bytes=1)
        with pytest.raises(AuditWriteError):
            log.require_written(checkin(9))
        assert log.try_write({"event": "unseal", "actor": "operator"}) is False
        assert log.dropped == 1
    finally:
        tmp_path.chmod(0o700)


def test_try_write_does_not_raise_when_the_record_cannot_be_written(tmp_path, monkeypatch):
    log = filled(tmp_path, 2)

    def broken(fd, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "write", broken)
    assert log.try_write({"event": "unseal", "actor": "operator"}) is False
    assert log.try_write({"event": "nonsense"}) is False  # even a bad entry never raises
    assert log.dropped == 2


def test_try_write_succeeds_normally(tmp_path):
    log = AuditLog(tmp_path)
    assert log.try_write({"event": "unseal", "actor": "operator"}) is True
    assert log.dropped == 0


def test_torn_final_line_is_dropped_on_the_next_append(tmp_path):
    log = filled(tmp_path, 3)
    path = segments(tmp_path)[0][1]
    with open(path, "ab") as handle:
        handle.write(b'{"seq":5,"half a wri')
    log.require_written(checkin(9))
    assert verify(tmp_path).status is Status.OK


def _append_many(path: str, tag: str) -> None:
    log = AuditLog(Path(path), Retention(segment_bytes=2000))
    for n in range(40):
        log.require_written(checkin(f"{tag}{n}"))


def test_two_processes_append_one_valid_chain(tmp_path):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_append_many, args=(str(tmp_path), tag)) for tag in "ab"]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
        assert proc.exitcode == 0
    result = verify(tmp_path)
    assert result.status in (Status.OK, Status.TRIMMED)
    assert result.entries == 81  # genesis + 2 * 40


def test_restore_entries_verify_linked_and_unlinked(tmp_path):
    old = filled(tmp_path / "old", 2)
    last = verify(tmp_path / "old")
    assert last.intact
    stored = old.require_written(checkin(3))

    linked = AuditLog(tmp_path / "linked")
    linked.require_written(
        {
            "event": "restore",
            "actor": "operator",
            "previous_seq": stored["seq"],
            "previous_hash": stored["hash"],
        }
    )
    unlinked = AuditLog(tmp_path / "unlinked")
    unlinked.require_written({"event": "restore", "actor": "operator", "unlinked": True})
    assert verify(tmp_path / "linked").status is Status.OK
    assert verify(tmp_path / "unlinked").status is Status.OK


def test_current_segment_is_never_trimmed_even_over_the_cap(tmp_path):
    from dmf_init.audit.retention import trim

    filled(tmp_path, 5, Retention(segment_bytes=10**6))
    far_future = datetime(2100, 1, 1, tzinfo=UTC)
    assert trim(tmp_path, Retention(max_bytes=1), far_future) == 0
    assert len(segments(tmp_path)) == 1
    assert verify(tmp_path).status is Status.OK


def test_quiet_record_is_trimmed_by_age_through_monthly_segments(tmp_path):
    # A few small entries a month for 15 months never reach the 1 MiB segment size.
    clock = {"now": datetime(2025, 1, 5, tzinfo=UTC)}
    log = AuditLog(tmp_path, Retention(), lambda: clock["now"])
    for _ in range(15):
        for n in range(3):
            log.require_written(checkin(n))
            clock["now"] += timedelta(days=1)
        clock["now"] += timedelta(days=27)
    found = segments(tmp_path)
    assert found[0][0] > 1  # the oldest months are gone
    # Trimming runs when a segment rolls, so measure age at the newest segment's start.
    started = json.loads(found[-1][1].read_text().splitlines()[0])["time"]
    cutoff = datetime.fromisoformat(started) - Retention().max_age
    for _, path in found[:-1]:
        newest = json.loads(path.read_text().splitlines()[-1])["time"]
        assert datetime.fromisoformat(newest) >= cutoff
    assert verify(tmp_path).status is Status.TRIMMED


def short_anchor_write(monkeypatch):
    """Make only the anchor write come up short; entry writes behave normally."""
    real = os.write

    def write(fd, data):
        return real(fd, data[:5]) if data.startswith(b'{"seq"') else real(fd, data)

    monkeypatch.setattr(os, "write", write)


def snapshot(path: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir())}


@pytest.mark.parametrize("earlier_trim", [False, True])
def test_short_anchor_write_leaves_the_record_unchanged(tmp_path, monkeypatch, earlier_trim):
    from dmf_init.audit.retention import trim

    filled(tmp_path, 40, Retention(segment_bytes=300, max_bytes=10**9))
    if earlier_trim:
        assert trim(tmp_path, Retention(max_bytes=4000)) > 0
        assert (tmp_path / ANCHOR_NAME).exists()
    before = snapshot(tmp_path)
    short_anchor_write(monkeypatch)
    with pytest.raises(OSError, match="short write"):
        trim(tmp_path, Retention(max_bytes=1))
    assert snapshot(tmp_path) == before  # no segment lost, anchor unchanged, no temp file
    assert verify(tmp_path).status is (Status.TRIMMED if earlier_trim else Status.OK)


def test_append_succeeds_when_the_trim_after_a_roll_fails(tmp_path, monkeypatch):
    log = filled(tmp_path, 10, Retention(segment_bytes=300, max_bytes=500))
    short_anchor_write(monkeypatch)
    for n in range(20):
        log.require_written(checkin(n))  # rolls several times; each trim fails
    assert not (tmp_path / (ANCHOR_NAME + ".tmp")).exists()
    monkeypatch.undo()
    assert verify(tmp_path).intact
    assert list(tmp_path.glob("segment-*"))[0].exists()


def trimmed_then_deleted(path: Path) -> AuditLog:
    log = filled(path, 60, SMALL)
    assert verify(path).status is Status.TRIMMED
    for _, segment in segments(path):
        segment.unlink()
    return log


def test_deleting_every_segment_is_a_gap_not_ok(tmp_path):
    trimmed_then_deleted(tmp_path)
    result = verify(tmp_path)
    assert result.status is Status.GAP
    assert result.seq is not None


def test_gate_refuses_to_resume_from_an_anchor_alone(tmp_path):
    log = trimmed_then_deleted(tmp_path)
    with pytest.raises(AuditWriteError):
        log.require_written(checkin(1))
    assert not list(tmp_path.glob("segment-*"))
    assert verify(tmp_path).status is Status.GAP


def test_unseal_path_counts_a_drop_when_the_record_was_deleted(tmp_path):
    log = trimmed_then_deleted(tmp_path)
    assert log.try_write({"event": "unseal", "actor": "operator"}) is False
    assert log.dropped == 1
    assert not list(tmp_path.glob("segment-*"))


def test_empty_directory_without_an_anchor_starts_a_fresh_chain(tmp_path):
    assert verify(tmp_path).status is Status.OK
    log = AuditLog(tmp_path / "new")
    log.require_written(checkin(1))
    assert verify(tmp_path / "new").status is Status.OK


def listing(path: Path) -> dict:
    return {p.name: p.stat().st_size for _, p in segments(path)}


def unseal() -> dict:
    return {"event": "unseal", "actor": "operator"}


def test_gate_refuses_after_a_retained_middle_segment_is_deleted(tmp_path):
    log = filled(tmp_path, 12, Retention(segment_bytes=300))
    found = segments(tmp_path)
    assert len(found) >= 3 and not (tmp_path / ANCHOR_NAME).exists()
    found[1][1].unlink()
    before = listing(tmp_path)
    with pytest.raises(AuditWriteError, match="gap"):
        log.require_written(checkin(99))
    assert listing(tmp_path) == before
    assert log.try_write(unseal()) is True  # a broken record never blocks an unseal
    assert log.dropped == 0


def test_gate_refuses_after_a_non_tail_entry_is_edited(tmp_path):
    log = filled(tmp_path, 12, Retention(segment_bytes=300))
    first = segments(tmp_path)[0][1]
    first.write_text(first.read_text().replace('"r0"', '"r7"'))
    before = listing(tmp_path)
    with pytest.raises(AuditWriteError, match="tamper"):
        log.require_written(checkin(99))
    assert listing(tmp_path) == before
    assert log.try_write(unseal()) is True


def test_gate_still_accepts_writes_on_a_trimmed_chain(tmp_path):
    log = filled(tmp_path, 60, SMALL)
    assert verify(tmp_path).status is Status.TRIMMED
    log.require_written(checkin(99))
    assert verify(tmp_path).intact
