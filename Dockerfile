# syntax=docker/dockerfile:1.7

FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-builder
WORKDIR /build

COPY frontend/package*.json ./frontend/
WORKDIR /build/frontend
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM --platform=$TARGETPLATFORM python:3.12-slim
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

# ansible-core (not the full `ansible` meta-package, which bundles hundreds of
# unused community collections ≈436MB). The 3 collections the bootstrap actually
# calls are installed from requirements-collections.yml below. rclone is gone —
# the dual-remote backup model was removed (backups are browser downloads now).
COPY requirements-collections.yml ./
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      age \
      ansible-core \
      apache2-utils \
      bind9-dnsutils \
      ca-certificates \
      curl \
      git \
      jq \
      openssh-client \
      openssl \
      python3 \
      python3-pip \
      unzip; \
    rm -rf /var/lib/apt/lists/*; \
    ansible-galaxy collection install -r requirements-collections.yml \
      -p /usr/share/ansible/collections; \
    rm -rf /root/.ansible/tmp /root/.cache

ARG OPENTOFU_VERSION=1.10.3
ARG SOPS_VERSION=3.10.2
ARG KUBECTL_VERSION=1.33.1
ARG HELM_VERSION=3.18.4

# Known-good experiment-phase set:
# opentofu 1.10.3 (cloud lanes), sops 3.10.2, kubectl 1.33.1, helm 3.18.4
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) arch="amd64" ;; \
      arm64) arch="arm64" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/tofu.zip "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_${arch}.zip"; \
    unzip -o /tmp/tofu.zip -d /usr/local/bin; \
    curl -fsSLo /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${arch}"; \
    chmod +x /usr/local/bin/sops; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz"; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    cp "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    chmod +x /usr/local/bin/helm; \
    rm -f /tmp/tofu.zip /tmp/helm.tar.gz; \
    rm -rf "/tmp/linux-${arch}"

RUN pip install --no-cache-dir .

# Install Ansible controller py-libs into /usr/bin/python3 (3.13 on trixie).
# Ansible's collections/modules import from /usr/bin/python3, NOT the 3.12
# /usr/local/bin/python that `pip install .` targets.
# PyYAML must be installed first with --ignore-installed because trixie ships
# apt-managed PyYAML 6.0.2 with no pip RECORD, blocking upgrade.
RUN set -eux; \
    /usr/bin/python3 -m pip --version; \
    /usr/bin/python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed PyYAML>=6.0.3; \
    /usr/bin/python3 -m pip install --no-cache-dir --break-system-packages jmespath netaddr passlib jsonpatch kubernetes yq; \
    /usr/bin/python3 -c "import jmespath,netaddr,passlib,jsonpatch,kubernetes,yaml; print('yaml', yaml.__version__)"; \
    rm -rf /root/.cache

CMD ["python", "-m", "dmf_init.main"]
