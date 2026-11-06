#!/usr/bin/env bash
# Pin a launcher's default image to a release.
#
#   scripts/stamp-launcher-image.sh <launcher-file> <tag>
#
# The release workflow runs this on a copy of bin/dmf-init before it computes
# the checksum, so a verified launcher starts the image from the same release
# instead of whatever :latest points to later. The image workflow pushes the
# semver tag without the leading "v", so the stamp drops it too.
#
# The script fails unless the marker line appears exactly once. A silent no-op
# would publish a launcher that is still pinned to :latest.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <launcher-file> <tag>" >&2
  exit 2
fi
file="$1"
tag="$2"

if ! [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "tag must look like v1.2.3, got: $tag" >&2
  exit 2
fi
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 2; }

image_repo="ghcr.io/dmfdeploy/dmf-init"
marker="DEFAULT_IMAGE=\"${image_repo}:latest\""
stamped="DEFAULT_IMAGE=\"${image_repo}:${tag#v}\""

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
