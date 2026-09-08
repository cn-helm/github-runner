#!/usr/bin/env bash
# Requires a running Docker daemon and an already pulled Noble image.
set -Eeuo pipefail
chart_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
smoke_name="github-runner-smoke-$(date +%s)-$$"
image="${SMOKE_IMAGE:-public.ecr.aws/ubuntu/ubuntu:noble}"
cleanup() {
    docker rm -f "$smoke_name-init" "$smoke_name-main" >/dev/null 2>&1 || true
    docker volume rm "$smoke_name-packages" >/dev/null
}
docker image inspect "$image" >/dev/null
docker volume create "$smoke_name-packages" >/dev/null
trap cleanup EXIT
security=(--user 0:0 --security-opt no-new-privileges --cap-drop ALL
          --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETUID --cap-add SETGID)
echo 'Downloading dependencies in the init container...'
docker run --rm --name "$smoke_name-init" "${security[@]}" \
    -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \
    -v "$smoke_name-packages:/packages" \
    -v "$chart_dir/scripts:/opt/runner-scripts:ro" \
    "$image" /bin/bash /opt/runner-scripts/download-dependencies.sh
echo 'Installing from shared packages in a separate, network-disabled container...'
docker run --rm --name "$smoke_name-main" --network none "${security[@]}" \
    -v "$smoke_name-packages:/packages:ro" \
    -v "$chart_dir/scripts:/opt/runner-scripts:ro" \
    -v "$chart_dir/tests:/smoke:ro" \
    "$image" /bin/bash /opt/runner-scripts/start-container.sh /bin/bash /smoke/container-runtime.sh
