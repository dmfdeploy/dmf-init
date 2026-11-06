#!/usr/bin/env bash
# Pin a launcher's default image to an exact image reference.
#
#   scripts/stamp-launcher-image.sh <launcher-file> <image-ref>
#
# <image-ref> is ghcr.io/dmfdeploy/dmf-init with a tag, a digest, or both:
#   ghcr.io/dmfdeploy/dmf-init:0.4.0
#   ghcr.io/dmfdeploy/dmf-init@sha256:<64 hex>
#   ghcr.io/dmfdeploy/dmf-init:0.4.0@sha256:<64 hex>
#
# scripts/build-launcher-release.sh runs this on a copy of bin/dmf-init before
# it computes the checksum, so a verified launcher starts the image that the
# release built instead of whatever :latest points to later.
#
# The script fails unless the marker line appears exactly once. A silent no-op
# would publish a launcher that is still pinned to :latest. On any failure the
# launcher file stays as it was.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <launcher-file> <image-ref>" >&2
  exit 2
fi
file="$1"
ref="$2"

image_repo="ghcr.io/dmfdeploy/dmf-init"
tag_re=':[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
digest_re='@sha256:[0-9a-f]{64}'
if ! [[ "$ref" =~ ^${image_repo//./\\.}(${tag_re})?(${digest_re})?$ ]] || [ "$ref" = "$image_repo" ]; then
  echo "image ref must be ${image_repo} with a :tag and/or @sha256:<64 hex> digest, got: $ref" >&2
  exit 2
fi
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 2; }

marker="DEFAULT_IMAGE=\"${image_repo}:latest\""
stamped="DEFAULT_IMAGE=\"${ref}\""

count="$(grep -cFx "$marker" "$file" || true)"
if [ "$count" -ne 1 ]; then
  echo "expected exactly one line '$marker' in $file, found $count" >&2
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$line" = "$marker" ]; then line="$stamped"; fi
  printf '%s\n' "$line"
done < "$file" > "$tmp"
cat "$tmp" > "$file"
