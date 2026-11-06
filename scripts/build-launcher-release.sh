#!/usr/bin/env bash
# Build the launcher release assets.
#
#   scripts/build-launcher-release.sh <image-ref> <outdir>
#
# Writes <outdir>/dmf-init (the launcher pinned to <image-ref>) and
# <outdir>/SHA256SUMS (one line for that file). The stamp happens before the
# checksum, so the checksum covers the bytes that users download. The release
# workflow calls this script and attaches both files unchanged; a test checks
# that order.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <image-ref> <outdir>" >&2
  exit 2
fi
ref="$1"
outdir="$2"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$outdir"
cp "$here/../bin/dmf-init" "$outdir/dmf-init"
"$here/stamp-launcher-image.sh" "$outdir/dmf-init" "$ref"

# Hash from inside the directory so the file name in SHA256SUMS is plain
# "dmf-init", which the documented install command requires.
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$outdir" && sha256sum dmf-init > SHA256SUMS)
else
  (cd "$outdir" && shasum -a 256 dmf-init > SHA256SUMS)
fi
