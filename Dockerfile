# voice-journey's cutover image: the 308 responder only (redirect/responder.py).
#
# Voice Journey moved to voice-lab (scshafe/voice-lab). For 90 days after the
# cutover this image answers every request to the old node with 308 to the
# same path on voice-lab (GET /healthz: 200 {"ok":true}, the deploy witness).
# Python standard library only: no dependencies, no build secret, no data.
#
#   docker build -t voice-journey .
#   docker build --target test .        # runs the responder's tests in the image

# python:3.12-alpine (Python 3.12.15, Alpine 3.24.2), multi-arch index digest
ARG PYTHON_IMAGE=python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4

FROM ${PYTHON_IMAGE} AS runtime
# Root-owned and read-only to the app; no bytecode written (read-only root).
WORKDIR /app
COPY --chmod=0444 redirect/responder.py /app/responder.py
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REDIRECT_PORT=8787
# nobody:nobody; the compose file says the same.
USER 65534:65534
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python3", "-B", "/app/responder.py", "--healthcheck"]
# REDIRECT_ORIGIN (required) and REDIRECT_HOST come from compose
# (deploy/stack/compose.yaml); without a valid REDIRECT_ORIGIN it exits 2.
CMD ["python3", "-B", "/app/responder.py"]

# ---- test target: `docker build --target test` runs the tests in the image ----
# Not the default target; nothing from this stage ships.
FROM runtime AS test
COPY --chmod=0444 redirect/test_responder.py /app/test_responder.py
RUN python3 -B -m unittest discover -s /app

# Make the shipped image the default target.
FROM runtime
