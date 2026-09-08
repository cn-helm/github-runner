#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 1001 && "$(id -g)" == 1001 ]]
[[ "$HOME" == /home/runner ]]
touch "$HOME/write-check"
id
git --version
curl --version
jq --version
tini --version
dpkg-query -W libicu74 libssl3t64 libkrb5-3 zlib1g libunwind8 liblttng-ust1t64
echo 'Public-image bootstrap passed: dependencies installed, UID/GID 1001, writable home.'
