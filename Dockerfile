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

ARG OPENTOFU_VERSION=1.10.3
ARG SOPS_VERSION=3.10.2
ARG KUBECTL_VERSION=1.33.1
ARG HELM_VERSION=3.18.4

# Known-good experiment-phase set:
# opentofu 1.10.3 (cloud lanes), sops 3.10.2, kubectl 1.33.1, helm 3.18.4
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
        tofu_sha=acf330602ec6ae29ba68dd5d8eb1f645811ae9809231ecdccd4774b21d5c79bc; \
        sops_sha=79b0f844237bd4b0446e4dc884dbc1765fc7dedc3968f743d5949c6f2e701739; \
        kubectl_sha=5de4e9f2266738fd112b721265a0c1cd7f4e5208b670f811861f699474a100a3; \
        helm_sha=f8180838c23d7c7d797b208861fecb591d9ce1690d8704ed1e4cb8e2add966c1 ;; \
      arm64) \
        tofu_sha=7011e9da95299dc8f8eaf4a54ebe066982ead04de778505dfbcaf406de25519a; \
        sops_sha=e91ddc04e6a78f5aed9e4fc347a279b539c43b74d99e6b8078e2f2f6f5b309f5; \
        kubectl_sha=d595d1a26b7444e0beb122e25750ee4524e74414bbde070b672b423139295ce6; \
        helm_sha=c0a45e67eef0c7416a8a8c9e9d5d2d30d70e4f4d3f7bea5de28241fffa8f3b89 ;; \
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
