#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() { printf 'runner: %s\n' "$*" >&2; exit 1; }
: "${GITHUB_REPO:?Set GITHUB_REPO to owner/repository}"
: "${RUNNER_NAME:?Set RUNNER_NAME}"
: "${RUNNER_INIT_VERSION:?Set RUNNER_INIT_VERSION}"
: "${RUNNER_SHA256:?Set RUNNER_SHA256}"
[[ "$GITHUB_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail 'Invalid repository.'
[[ "$RUNNER_INIT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Invalid initial version.'
[[ "$RUNNER_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || fail 'Invalid SHA256.'
[[ "$(uname -m)" == x86_64 ]] || fail 'This image configuration requires Linux x64.'

persistent="${RUNNER_PERSISTENT_DIR:-/persistent}"
runner_dir="$persistent/gh-runner"
work_dir="${RUNNER_WORK_DIR:-_work}"
# Restrict work to a single child directory of the persistent installation.
[[ "$work_dir" =~ ^[A-Za-z0-9_-][A-Za-z0-9_.-]*$ ]] || fail 'Work directory must be a simple relative directory name.'
case "$work_dir" in bin|externals) fail 'Work directory conflicts with runner files.' ;; esac
mkdir -p "$persistent"
exec 9>"$persistent/.runner.lock"
flock -n 9 || fail 'Another runner already owns this PVC.'

if [[ ! -d "$runner_dir" ]]; then
    # This fixed staging directory is only touched while holding the PVC lock.
    stage="$persistent/.runner-install"
    rm -rf -- "$stage"
    mkdir -p "$stage/package"
    archive="$stage/runner.tar.gz"
    url="https://github.com/actions/runner/releases/download/v${RUNNER_INIT_VERSION}/actions-runner-linux-x64-${RUNNER_INIT_VERSION}.tar.gz"
    printf 'Installing initial runner version %s\n' "$RUNNER_INIT_VERSION"
    curl --fail --location --show-error --silent --connect-timeout 30 \
        --max-time 900 --output "$archive" "$url"
    printf '%s  %s\n' "$RUNNER_SHA256" "$archive" | sha256sum --check --status || \
        fail 'Runner archive SHA256 mismatch; installation was not activated.'
    tar -xzf "$archive" -C "$stage/package"
    [[ -x "$stage/package/config.sh" && -x "$stage/package/run.sh" ]] || fail 'Incomplete runner archive.'
    touch "$stage/package/.installation-complete"
    mv -- "$stage/package" "$runner_dir"
    rm -rf -- "$stage"
fi

[[ -f "$runner_dir/.installation-complete" && -x "$runner_dir/run.sh" ]] || \
    fail 'Incomplete or unmanaged installation; inspect PVC before recovery.'
cd "$runner_dir"
settings=$(printf '%s\n' "$GITHUB_REPO" "$RUNNER_NAME" "${RUNNER_LABELS:-k8s,ubuntu24}" "$work_dir")
if [[ -f .runner ]]; then
    [[ -s .credentials && -s .credentials_rsaparams && -f .chart-registration ]] || \
        fail 'Incomplete registration; inspect PVC and re-register explicitly.'
    [[ "$(cat .chart-registration)" == "$settings" ]] || \
        fail 'Repository/name/labels/workDir changed; restore values or explicitly re-register.'
else
    [[ ! -e .credentials && ! -e .credentials_rsaparams && ! -e .chart-registration ]] || \
        fail 'Partial registration found; inspect PVC and re-register explicitly.'
    [[ -n "${GITHUB_RUNNER_TOKEN:-}" ]] || fail 'First registration requires a valid token in the configured Secret.'
    ./config.sh --unattended \
        --url "https://github.com/$GITHUB_REPO" \
        --token "$GITHUB_RUNNER_TOKEN" \
        --name "$RUNNER_NAME" \
        --labels "${RUNNER_LABELS:-k8s,ubuntu24}" \
        --work "$work_dir"
    printf '%s\n' "$settings" > .chart-registration.tmp
    mv .chart-registration.tmp .chart-registration
fi

# Do not expose the short-lived registration token to workflow processes.
unset GITHUB_RUNNER_TOKEN
# The official wrapper forwards TERM/INT and restarts the listener after updates.
export RUNNER_MANUALLY_TRAP_SIG=1
exec ./run.sh
