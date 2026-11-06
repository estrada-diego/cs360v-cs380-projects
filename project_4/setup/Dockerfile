# The container you work in for Project 4.
#
# ubuntu:24.04 is multi-arch, so this builds natively on Apple Silicon or Intel.
FROM ubuntu:24.04

# A C compiler (to build runner.c), Python 3 (the platform and the tests), curl
# (to send requests to the platform by hand), and procps (ps, pkill).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        make \
        python3 \
        curl \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Run as the image's non-root 'ubuntu' user. Everything the platform and the
# tests write goes to /tmp, not the mounted /work.
WORKDIR /work
USER ubuntu

# Keep the container running so you can `docker compose exec` into it.
CMD ["sleep", "infinity"]
