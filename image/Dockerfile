FROM public.ecr.aws/ubuntu/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq openssh-client tar gzip unzip \
        tini util-linux libicu74 libssl3t64 libkrb5-3 zlib1g libunwind8 liblttng-ust1t64 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 runner \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash runner \
    && mkdir /persistent \
    && chown runner:runner /persistent

COPY --chmod=0755 entrypoint.sh /usr/local/bin/runner-entrypoint
ENV HOME=/home/runner
USER 1001:1001
WORKDIR /persistent
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/runner-entrypoint"]
