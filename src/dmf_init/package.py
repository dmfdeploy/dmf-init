"""Recovery-package assembly for the create flow's Finish screen.

Bundles everything the operator must keep before deleting the container into
one zip: the latest (checkpoint #3) backup artifact, the cluster CA
certificate, a README with the workstation reference material, and a MANIFEST
with sha256 digests so the contents can be verified after download.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backup import validate_env_id


class PackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageResult:
    filename: str
    data: bytes
    sha256: str
    artifact_name: str


def find_latest_artifact(data_root: Path, env_id: str) -> Path:
    """Latest backup artifact for env_id (names embed a UTC stamp, so
    lexicographic order is chronological — the newest is checkpoint #3)."""
    validate_env_id(env_id)
    artifacts_dir = data_root / "artifacts"
    candidates = sorted(artifacts_dir.glob(f"dmf-backup-{env_id}-*.tar.age"))
    if not candidates:
        raise PackageError(f"no backup artifact found for env_id={env_id}")
    return candidates[-1]


def _readme(
    env_id: str,
    artifact_name: str,
    ca_payload: dict[str, Any],
    hosts_payload: dict[str, Any],
) -> str:
    hosts_entries = "\n".join(hosts_payload.get("entries") or ["(unavailable at package time)"])
    ca_filename = ca_payload.get("filename", "dmf-ca.crt")
    ca_section = (
        f"""The cluster's local CA certificate is included as `{ca_filename}`.
Trusting it is strongly recommended: without it every cluster service shows
certificate warnings, and on desktop Chrome/Chromium passkey enrollment fails
silently. Install per OS:

- Windows (admin):  certutil -addstore -f "Root" {ca_filename}
- macOS:            sudo security add-trusted-cert -d -r trustRoot \\
                      -k /Library/Keychains/System.keychain {ca_filename}
- Debian/Ubuntu:    sudo cp {ca_filename} /usr/local/share/ca-certificates/ \\
                      && sudo update-ca-certificates
- Fedora/RHEL:      sudo cp {ca_filename} /etc/pki/ca-trust/source/anchors/ \\
                      && sudo update-ca-trust
- Firefox:          Settings > Privacy & Security > Certificates >
                    View Certificates > Authorities > Import (own store;
                    bounds the blast radius)

Restart the browser afterwards. To remove later, delete the 'DMF Local CA'
entry from the certificate store."""
        if ca_payload.get("present")
        else "The CA certificate was not reachable when this package was "
        "assembled. Fetch it later from a Manage session, or re-download "
        "the package."
    )
    return f"""# DMF recovery package — {env_id}

Assembled {datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}. Keep this file
AND your passphrase; together they are the only way to manage or recover this
environment once the init container is deleted. The passphrase is NOT in this
package — store it separately (password manager).

## Contents

- `{artifact_name}` — the encrypted environment backup (checkpoint #3,
  sealed after successful verification). Restore it from the Manage screen
  of any dmf-init container: upload + passphrase.
- `{ca_payload.get("filename", "dmf-ca.crt")}` — the cluster's local CA certificate (public).
- `MANIFEST.json` — sha256 digests of the files above.

## Workstation reference

### Hosts mapping

Cluster hostnames must resolve to the node IP on every workstation that uses
the cluster ({hosts_payload.get("node_ip") or "node IP unavailable"}):

```
{hosts_entries}
```

### CA certificate

{ca_section}
"""


def build_package(
    data_root: Path,
    env_id: str,
    ca_payload: dict[str, Any],
    hosts_payload: dict[str, Any],
) -> PackageResult:
    artifact_path = find_latest_artifact(data_root, env_id)
    artifact_bytes = artifact_path.read_bytes()
    ca_pem = str(ca_payload.get("pem") or "")
    ca_filename = str(ca_payload.get("filename") or "dmf-ca.crt")
    readme = _readme(env_id, artifact_path.name, ca_payload, hosts_payload)

    files: list[tuple[str, bytes]] = [(artifact_path.name, artifact_bytes)]
    if ca_payload.get("present") and ca_pem:
        files.append((ca_filename, (ca_pem.rstrip("\n") + "\n").encode("utf-8")))
    files.append(("README.md", readme.encode("utf-8")))

    manifest = {
        "env_id": env_id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": [
            {"name": name, "sha256": hashlib.sha256(blob).hexdigest()}
            for name, blob in files
        ],
    }
    files.append(
        ("MANIFEST.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, blob in files:
            # The .tar.age artifact is already encrypted (incompressible);
            # store it as-is, deflate the small text files.
            method = zipfile.ZIP_STORED if name.endswith(".tar.age") else zipfile.ZIP_DEFLATED
            archive.writestr(name, blob, compress_type=method)
    data = buffer.getvalue()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return PackageResult(
        filename=f"dmf-package-{env_id}-{stamp}.zip",
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        artifact_name=artifact_path.name,
    )
