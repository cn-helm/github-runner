#!/usr/bin/env bash
set -Eeuo pipefail
# Uses an already-pulled image, no registry login, network access or ECR push.
image=public.ecr.aws/docker/library/docker:dind
docker image inspect "$image" >/dev/null
name="github-runner-dind-smoke-$$"
created=false
cleanup() {
    if [[ "$created" == true ]]; then
        docker rm -fv "$name" >/dev/null
    fi
}
trap cleanup EXIT
docker run -d --pull=never --name "$name" --privileged --network none \
    -e DOCKER_TLS_CERTDIR= \
    -v /var/run/docker -v /var/lib/docker \
    "$image" dockerd --host=unix:///var/run/docker/docker.sock --group=1001 >/dev/null
created=true
ready=false
for attempt in {1..30}; do
    if docker exec --user 1001:1001 "$name" \
        docker --host=unix:///var/run/docker/docker.sock info >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    docker logs "$name"
    exit 1
fi
# A separate unprivileged client shares only the socket directory.
docker run --rm -i --pull=never --network none --user 1001:1001 \
    -e HOME=/tmp -e DOCKER_CONFIG=/tmp/.docker \
    --volumes-from "$name":ro --entrypoint docker \
    "$image" --host=unix:///var/run/docker/docker.sock build -t runner-smoke - <<'EOF'
FROM scratch
LABEL smoke=runner-dind
EOF
echo 'DinD passed: UID/GID 1001 can connect and build without registry access.'
