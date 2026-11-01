# Runtime dependencies come from the same immutable upstream image as the lab.
# Only the test-tool closure is added, with exact versions and wheel hashes from
# the pinned uv.lock. Network is needed here, never during patch validation.
FROM sissbruecker/linkding@sha256:5e37e0c0a3cb915ab02e432a0c4026e8ad8b3a0f27c9ca9b7cc4feff7d2eefce
LABEL org.opencontainers.image.source="https://github.com/sissbruecker/linkding"
LABEL org.opencontainers.image.revision="f279015767b734c8f9a17c402adaa68bcd9f8d90"
LABEL org.aura.lab="aura-ops-lab"
LABEL org.aura.role="patch-test-profile"
COPY test-requirements.txt /tmp/aura-test-requirements.txt
RUN /usr/local/bin/python -m pip --python /etc/linkding/.venv/bin/python install \
    --no-cache-dir --disable-pip-version-check --only-binary=:all: \
    --require-hashes --no-deps --requirement /tmp/aura-test-requirements.txt \
    && /etc/linkding/.venv/bin/python -m pytest --version \
    && rm /tmp/aura-test-requirements.txt
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
HEALTHCHECK NONE
