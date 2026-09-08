#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
packages="${RUNNER_PACKAGE_DIR:-/packages}"
[[ -f "$packages/.complete" ]] || { echo 'Dependency initialization is incomplete.' >&2; exit 1; }
# Fail clearly if the mutable tag resolved to different base images in one Pod.
sha256sum --check --status "$packages/base.sha256" || {
    echo 'Base image differs from initContainer; pin image.digest and recreate the Pod.' >&2
    exit 1
}
sha256sum --check --status "$packages/packages.sha256"
shopt -s nullglob
archives=("$packages"/*.deb)
(( ${#archives[@]} > 0 )) || { echo 'Dependency cache is empty.' >&2; exit 1; }
# Resolve local packages in dependency order, with downloads explicitly disabled.
# APT's no-download path requires archives to exist in its own writable cache.
cache="${RUNNER_APT_CACHE_DIR:-/var/cache/apt/archives}"
mkdir -p "$cache/partial"
cp -- "${archives[@]}" "$cache/"
apt-get -o "Dir::Cache::archives=$cache" --no-download --no-install-recommends \
    install -y "$cache"/*.deb
groupadd --gid 1001 runner
useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash runner
export HOME=/home/runner USER=runner LOGNAME=runner
# UID change clears root's effective capabilities; no-new-privs prevents regain.
exec setpriv --reuid=1001 --regid=1001 --init-groups --no-new-privs \
    /usr/bin/tini -- "$@"
