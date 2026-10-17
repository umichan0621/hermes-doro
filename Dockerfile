ARG HERMES_IMG=nousresearch/hermes-agent:latest
FROM ${HERMES_IMG}

# Lark adapter SDKs and edge-tts are lazy opt-in deps not baked into
# [all]/[messaging]; install them into the sealed venv so the gateway boots
# without runtime PyPI access. edge-tts==7.2.7 mirrors the LAZY_DEPS pin for
# 'tts.edge' so lazy_deps.ensure() sees it satisfied and skips the runtime
# install (version must match exactly, else it re-installs into lazy-packages).
USER root

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    ripgrep \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Newer base images (>=2026-10) ship a uv-managed venv with no pip and no uv
# binary — bootstrap pip via ensurepip, then install into the sealed venv.
RUN /opt/hermes/.venv/bin/python -m ensurepip \
    && /opt/hermes/.venv/bin/python -m pip install --no-cache-dir lark-oapi websockets edge-tts==7.2.7

# lark-cli (github.com/larksuite/cli): official Lark CLI backing the
# lark-* agent skills. Static Go binary (CGO_ENABLED=0), version-pinned and
# sha256-verified, installed system-wide so the hermes runtime user can exec
# it. Skills and OAuth/app credentials live under /opt/data (runtime volume),
# NOT in the image — they survive rebuilds and are installed once at runtime.
# Upgrade: bump the three ARGs below (checksums from the release's checksums.txt).
ARG LARK_CLI_VERSION=1.0.98
ARG LARK_CLI_SHA256_AMD64=4a12dcb7a0aab25986d8bcd046cb45b08278531a9aac3a12b53cf3d94738fe80
ARG LARK_CLI_SHA256_ARM64=f798b26ce4b369d70411945e7f66bbe4382c411bc47d81950a662de7b69b41ed
RUN set -eux; \
    case "$(uname -m)" in \
    x86_64)  lark_arch=amd64; lark_sha="$LARK_CLI_SHA256_AMD64" ;; \
    aarch64) lark_arch=arm64; lark_sha="$LARK_CLI_SHA256_ARM64" ;; \
    *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    tgz="lark-cli-${LARK_CLI_VERSION}-linux-${lark_arch}.tar.gz"; \
    curl -fsSL -o "/tmp/${tgz}" "https://github.com/larksuite/cli/releases/download/v${LARK_CLI_VERSION}/${tgz}"; \
    echo "${lark_sha}  /tmp/${tgz}" | sha256sum -c -; \
    tar -xzf "/tmp/${tgz}" -C /usr/local/bin lark-cli; \
    chmod 0755 /usr/local/bin/lark-cli; \
    /usr/local/bin/lark-cli --version; \
    rm -f "/tmp/${tgz}"

# Do NOT add `USER hermes` here: the official image runs s6-overlay as PID 1
# (root) and drops privileges to the hermes uid internally; forcing a non-root
# USER would break the first-boot volume chown performed by the entrypoint.
