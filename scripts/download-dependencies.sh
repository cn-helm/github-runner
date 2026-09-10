#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}"
export https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
export no_proxy="${no_proxy:-${NO_PROXY:-}}"
packages="${RUNNER_PACKAGE_DIR:-/packages}"
mkdir -p "$packages/partial"
# An initContainer retry must not mix packages from different apt resolutions.
rm -f -- "$packages"/*.deb "$packages/.complete" "$packages/awscliv2.zip" "$packages/awscliv2.zip.part"
sha256sum /var/lib/dpkg/status > "$packages/base.sha256"
apt-get -o Acquire::Retries=0 -o APT::Update::Error-Mode=any update
apt-get -o Acquire::Retries=0 -o "Dir::Cache::archives=$packages" \
    install --download-only --no-install-recommends -y \
    bash ca-certificates curl git jq openssh-client tar gzip unzip \
    docker.io docker-buildx \
    tini util-linux libicu74 libssl3t64 libkrb5-3 zlib1g libunwind8 liblttng-ust1t64
shopt -s nullglob
archives=("$packages"/*.deb)
(( ${#archives[@]} > 0 )) || { echo 'No dependency packages were downloaded.' >&2; exit 1; }
# Install download tools only after caching the full dependency set, so packages
# absent from the pristine runner image are still included in the shared cache.
apt-get -o "Dir::Cache::archives=$packages" --no-download --no-install-recommends \
    install -y ca-certificates curl unzip
curl --fail --location --show-error --silent --connect-timeout 30 --max-time 900 \
    https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip \
    --output "$packages/awscliv2.zip.part"
mv -- "$packages/awscliv2.zip.part" "$packages/awscliv2.zip"
# Integrity of the shared cache, not an independent publisher signature check.
sha256sum "${archives[@]}" "$packages/awscliv2.zip" > "$packages/packages.sha256"
touch "$packages/.complete"
