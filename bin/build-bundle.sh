#!/usr/bin/env bash
# build-bundle.sh — build a SELF-CONTAINED dmf-init appliance image (the 6 runtime
# repos baked in) and export it as a portable `docker load` tarball. No registry,
# no source checkout, no bind-mounts needed on the target host:
#   docker load -i <tarball>
#   docker run --rm -p 127.0.0.1:8000:8000 --tmpfs /tmp/dmf-init-data:exec dmf-init:bundle
#   # open the http://localhost:8000/?token=... printed in the logs
#
# REPO SOURCE (two modes):
#   default (fresh)  clone each of the 6 repos shallow at --ref from origin — the
#                    reproducible, always-latest, CI-friendly path. A clean clone
#                    has only committed content (no .terraform/node_modules cruft).
#   --local          rsync the local sibling working trees instead (dev: bake
#                    in-flight uncommitted work). Drops build cruft via excludes.
#
# WHEN TO USE THE BUNDLE: pick this only for a target host with no network access
# at runtime (air-gapped). The canonical Dockerfile instead fetches the repos at
# startup — public-safe now the repos are public — so on a networked host prefer
# `docker run ghcr.io/dmfdeploy/dmf-init:latest` over building a tarball.
#
# Usage:
#   bin/build-bundle.sh [-o OUT_DIR] [--ref REF] [--local] \
#                       [--repo-base-url URL] [--repo-token TOK]
# Env: DMF_REPO_TOKEN (preferred over --repo-token; http(s) auth for private clones)

set -euo pipefail

# The Dockerfile uses BuildKit-only automatic args (FROM --platform=$BUILDPLATFORM);
# classic builders (e.g. Debian docker.io's default) leave them empty and fail.
# colima enables BuildKit by default; force it everywhere so CI matches local.
export DOCKER_BUILDKIT=1

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # dmf-init
UMBRELLA="$(cd "$REPO_DIR/.." && pwd)"                        # dmfdeploy umbrella
VERSION="$(tr -d '[:space:]' < "$REPO_DIR/VERSION" 2>/dev/null || echo 0.0.0)"
IMAGE="dmf-init:bundle"
BASE_IMAGE="dmf-init:bundle-base"
DATA_ROOT="/tmp/dmf-init-data"
REPOS=(dmf-env dmf-infra dmf-runbooks dmf-cms dmf-media dmf-promsd)

OUT_DIR="$PWD"; MODE="fresh"; REF="main"; REPO_BASE_URL=""; REPO_TOKEN="${DMF_REPO_TOKEN:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--output)      OUT_DIR="$2"; shift 2 ;;
    --ref)            REF="$2"; shift 2 ;;
    --local)          MODE="local"; shift ;;
    --repo-base-url)  REPO_BASE_URL="${2%/}"; shift 2 ;;
    --repo-token)     REPO_TOKEN="$2"; shift 2 ;;
    -h|--help)        sed -n '2,30p' "$0"; exit 0 ;;
    *)                OUT_DIR="$1"; shift ;;   # back-compat positional out-dir
  esac
done
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
TARBALL="${OUT_DIR}/dmf-init-bundle-${VERSION}.tar.gz"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon not reachable (start dockerd / 'colima start')." >&2
  exit 1
fi

# Resolve the clone URL for repo $1; injects DMF_REPO_TOKEN into http(s) if set.
_repo_url() {
  local name="$1" url
  if [ -n "$REPO_BASE_URL" ]; then
    url="${REPO_BASE_URL}/${name}.git"
  else
    url="$(git -C "${UMBRELLA}/${name}" remote get-url origin 2>/dev/null)" \
      || { echo "  no origin for ${name} (need --repo-base-url)" >&2; return 1; }
  fi
  if [ -n "$REPO_TOKEN" ] && [ "${url#http}" != "$url" ]; then
    url="$(printf '%s' "$url" | sed -E "s#^(https?://)#\1oauth2:${REPO_TOKEN}@#")"
  fi
  printf '%s' "$url"
}
# Strip any embedded credentials before recording/printing a URL.
_scrub_url() { printf '%s' "$1" | sed -E 's#(https?://)[^/@]*@#\1#'; }

echo "==> 1/4 building public-safe base image: ${BASE_IMAGE}"
docker build -t "${BASE_IMAGE}" "${REPO_DIR}"

stage="$(mktemp -d)"
trap 'rm -rf "${stage}"' EXIT
mkdir -p "${stage}/repos"
prov="${stage}/build-provenance.json"
printf '{\n  "image": "%s",\n  "version": "%s",\n  "built_at": "%s",\n  "mode": "%s",\n  "ref": "%s",\n  "repos": [\n' \
  "$IMAGE" "$VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$REF" > "$prov"

echo "==> 2/4 staging repos (mode=${MODE}, ref=${REF})"
i=0
for r in "${REPOS[@]}"; do
  if [ "$MODE" = "local" ]; then
    src="${UMBRELLA}/${r}"
    [ -d "${src}/.git" ] || { echo "  MISSING local repo: ${src}" >&2; exit 1; }
    echo "    + ${r} (local working tree)"
    rsync -a --exclude='node_modules/' --exclude='.venv/' --exclude='dist/' \
      --exclude='__pycache__/' --exclude='.terraform/' "${src}/" "${stage}/repos/${r}/"
    src_url="$(_scrub_url "$(git -C "${src}" remote get-url origin 2>/dev/null || echo local)")"
  else
    url="$(_repo_url "$r")"
    echo "    + ${r} (clone $(_scrub_url "$url") @ ${REF})"
    git clone --quiet --depth 1 --branch "$REF" "$url" "${stage}/repos/${r}"
    src_url="$(_scrub_url "$url")"
  fi
  sha="$(git -C "${stage}/repos/${r}" rev-parse HEAD)"
  i=$((i+1)); sep=','; [ "$i" -eq "${#REPOS[@]}" ] && sep=''
  printf '    {"name": "%s", "ref": "%s", "sha": "%s", "source": "%s"}%s\n' \
    "$r" "$REF" "$sha" "$src_url" "$sep" >> "$prov"
done
printf '  ]\n}\n' >> "$prov"

# COPY (unlike `docker cp`) creates intermediate dirs.
cat > "${stage}/Dockerfile" <<EOF
FROM ${BASE_IMAGE}
COPY repos ${DATA_ROOT}/repos
COPY build-provenance.json ${DATA_ROOT}/provenance/build.json
EOF

echo "==> 3/4 baking repos + provenance into ${IMAGE}"
docker build -t "${IMAGE}" "${stage}"

echo "==> 4/4 exporting portable tarball (can take a minute)"
docker save "${IMAGE}" | gzip > "${TARBALL}"

echo
echo "baked repo provenance:"
sed -n '/"repos"/,/]/p' "$prov" | grep -oE '"name": "[^"]+"|"sha": "[^"]{7}' | paste - - | sed 's/^/  /'
echo
echo "done: ${TARBALL}  ($(du -h "${TARBALL}" | cut -f1))"
echo "  docker load -i $(basename "${TARBALL}")"
echo "  docker run --rm -p 127.0.0.1:8000:8000 --tmpfs /tmp/dmf-init-data:exec ${IMAGE}"
echo "  (--tmpfs :exec is required: env secrets stay in RAM, never on host disk — ADR-0044)"
echo "  (HTTPS opt-in for non-localhost: add -e DMF_TLS_ENABLED=true)"
