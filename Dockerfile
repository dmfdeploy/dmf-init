# syntax=docker/dockerfile:1.7

FROM --platform=$BUILDPLATFORM node:26-slim AS frontend-builder
WORKDIR /build

COPY frontend/package*.json ./frontend/
WORKDIR /build/frontend
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM --platform=$TARGETPLATFORM python:3.15-slim
LABEL org.opencontainers.image.title="dmf-init" \
      org.opencontainers.image.description="DMF Init local bootstrap container"

# Bind 0.0.0.0 INSIDE the container so `docker run -p 127.0.0.1:8000:8000` works
# with no extra flags. Loopback-only safety lives in the publish mapping
# (-p 127.0.0.1:…), NOT the in-container bind — never publish to a non-loopback
# host interface.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DMF_DATA_ROOT=/tmp/dmf-init-data \
    DMF_BIND_HOST=0.0.0.0 \
    DMF_BIND_PORT=8000 \
    DMF_LAUNCH_TOKEN_TTL_SECONDS=1800 \
    DMF_SESSION_TTL_SECONDS=43200

WORKDIR /app

COPY pyproject.toml README.md VERSION ./
COPY src ./src
COPY --from=frontend-builder /build/src/dmf_init/static/app ./src/dmf_init/static/app

# The controller's ansible-core is the exact pin in requirements-ansible-core.txt,
# installed with pip into this image's /usr/local Python. `ansible-playbook` on PATH is
# that install. It is not the full `ansible` meta-package, which bundles hundreds of
# unused community collections (about 436MB). The 3 collections the bootstrap actually
# calls are installed from requirements-collections.yml.
COPY requirements-ansible-core.txt requirements-collections.yml ./
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      age \
      apache2-utils \
      bind9-dnsutils \
      ca-certificates \
      curl \
      git \
      jq \
      openssh-client \
      openssl \
      unzip; \
    rm -rf /var/lib/apt/lists/*

# The packages named here are pinned to exact versions. Their own dependencies
# resolve at build time, so a rebuild can pick up newer transitive versions.
# Dependabot does not track the versions in this command; bump them by hand.
# The Python libraries that the playbooks and collections import on the controller go
# into the same /usr/local Python that runs ansible-playbook.
RUN set -eux; \
    pip install --no-cache-dir -r requirements-ansible-core.txt \
      "PyYAML==6.0.4" "jmespath==1.1.0" "netaddr==1.3.0" "passlib==1.7.4" \
      "jsonpatch==1.48" "kubernetes==37.0.3" "yq==4.4.5"; \
    ansible-galaxy collection install -r requirements-collections.yml \
      -p /usr/share/ansible/collections; \
    python -c "import jmespath,netaddr,passlib,jsonpatch,kubernetes,yaml; print('yaml', yaml.__version__)"; \
    rm -rf /root/.ansible/tmp /root/.cache

ARG OPENTOFU_VERSION=1.13.9
ARG SOPS_VERSION=3.13.3
ARG KUBECTL_VERSION=1.37.2
ARG HELM_VERSION=4.3.2

# Known-good experiment-phase set:
# opentofu 1.13.9 (cloud lanes), sops 3.13.3, kubectl 1.37.2, helm 4.3.2
#
# Each download is checked against a pinned SHA-256 before it is used. When you
# bump a version, replace its checksums from the upstream files named here.
#   tofu:    tofu_<version>_SHA256SUMS in the OpenTofu GitHub release
#   sops:    sops-v<version>.checksums.txt in the sops GitHub release
#   kubectl: <binary>.sha256 beside the binary on dl.k8s.io
#   helm:    <tarball>.sha256sum beside the tarball on get.helm.sh
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) \
        tofu_sha=bc44eb6b06446ba83bd4072114891987f590f66cca712737a7f1147c50c539ce; \
        sops_sha=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b; \
        kubectl_sha=c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4; \
        helm_sha=d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983 ;; \
      arm64) \
        tofu_sha=04f7c93828b859d420269ed30f923ffebf09157b5d7fb95bf8d88a4d208b7fa3; \
        sops_sha=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77; \
        kubectl_sha=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509; \
        helm_sha=df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    fetch() { \
      curl -fsSLo "$2" "$1"; \
      actual="$(sha256sum "$2" | cut -d' ' -f1)"; \
      if [ "$actual" != "$3" ]; then \
        echo "checksum mismatch for $1: expected $3, got $actual" >&2; exit 1; \
      fi; \
    }; \
    fetch "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_${arch}.zip" /tmp/tofu.zip "$tofu_sha"; \
    unzip -o /tmp/tofu.zip -d /usr/local/bin; \
    fetch "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${arch}" /usr/local/bin/sops "$sops_sha"; \
    chmod +x /usr/local/bin/sops; \
    fetch "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" /usr/local/bin/kubectl "$kubectl_sha"; \
    chmod +x /usr/local/bin/kubectl; \
    fetch "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz" /tmp/helm.tar.gz "$helm_sha"; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    cp "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    chmod +x /usr/local/bin/helm; \
    rm -f /tmp/tofu.zip /tmp/helm.tar.gz; \
    rm -rf "/tmp/linux-${arch}"

RUN pip install --no-cache-dir .

CMD ["python", "-m", "dmf_init.main"]
