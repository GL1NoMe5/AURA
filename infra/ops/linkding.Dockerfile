# The runtime, compiled static files and dependencies are an immutable upstream
# v1.46.2 image. Both candidates receive the same source overlay and runtime.
# No application dependencies are changed by the evaluator reference patch.
FROM sissbruecker/linkding@sha256:5e37e0c0a3cb915ab02e432a0c4026e8ad8b3a0f27c9ca9b7cc4feff7d2eefce
LABEL org.opencontainers.image.source="https://github.com/sissbruecker/linkding"
LABEL org.opencontainers.image.revision="f279015767b734c8f9a17c402adaa68bcd9f8d90"
LABEL org.aura.lab="aura-ops-lab"
COPY bookmarks /etc/linkding/bookmarks
