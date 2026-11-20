"""The audit record that dmf-infra's token revoker reads, written by this writer.

The revoker (dmf-infra, roles/stack/operator/openbao/files/revoker/revoker.py) has its own
copy of the chain parser. dmf-infra keeps two records in tests/fixtures/audit-record/ that
the revoker's tests parse. This test writes the same records with the real writer and
compares them byte for byte, so a change to the entry format, the hash or the segment
layout fails here until the fixtures change, and the fixtures change only together with the
revoker. Set DMF_TEST_DMF_INFRA_REPO to a dmf-infra checkout to run it.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dmf_init.audit import AuditLog, Retention, Status, verify

FIXTURES = Path("k3s-lab-bootstrap/tests/fixtures/audit-record")
START = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def events() -> list[dict]:
    """An install, a closed request with its two accessors, and a request that is still open."""
    return [
        {"event": "install", "actor": "installer", "initial_token_revoked": True},
        {"event": "intent", "request_id": "r-closed", "actor": "approver", "role": "operator",
         "session": "s1", "catalogue_entry": "ops-admin-password-rotation"},
        {"event": "minted", "request_id": "r-closed", "session": "s1",
         "accessors": ["acc-closed-1", "acc-closed-2"]},
        {"event": "closed", "request_id": "r-closed", "outcome": "ok"},
        {"event": "intent", "request_id": "r-open", "actor": "approver", "role": "operator",
         "session": "s2", "catalogue_entry": "ops-admin-password-rotation"},
        {"event": "minted", "request_id": "r-open", "session": "s2",
         "accessors": ["acc-open-1", "acc-open-2"]},
    ]


def write(directory: Path, policy: Retention) -> None:
    ticks = iter(START + timedelta(seconds=n) for n in range(1000))
    log = AuditLog(directory, policy, clock=lambda: next(ticks))
    for event in events():
        log.require_written(event)


RECORDS = {
    # Two segments: the size limit rolls the segment, and the second links to the first.
    "rolled": Retention(segment_bytes=900),
    # The oldest segment is trimmed, so the record starts after an anchor.
    "trimmed": Retention(segment_bytes=500, max_bytes=1500),
}


@pytest.mark.parametrize("name", RECORDS)
def test_the_writer_produces_the_record_that_the_revoker_parses(tmp_path, name):
    checkout = os.getenv("DMF_TEST_DMF_INFRA_REPO")
    if not checkout:
        pytest.skip("DMF_TEST_DMF_INFRA_REPO is not set")
    fixture = Path(checkout) / FIXTURES / name
    if not fixture.is_dir():
        pytest.skip(f"no fixture at {fixture}")

    write(tmp_path, RECORDS[name])

    assert verify(tmp_path).status in (Status.OK, Status.TRIMMED)
    made = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != ".lock"}
    kept = {p.name: p.read_bytes() for p in fixture.iterdir()}
    assert made == kept
