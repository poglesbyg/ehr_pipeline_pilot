# Standard library only: no pip install step, no third-party packages to scan or patch.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 bridge
WORKDIR /app
COPY --chown=bridge:bridge hl7bridge/ hl7bridge/
COPY --chown=bridge:bridge samples/ samples/
COPY --chown=bridge:bridge eval/ eval/

USER bridge
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080 2575

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/fhir/metadata', timeout=2)"

# 0.0.0.0 inside the container only; publish ports to localhost or a private network.
# Drop --seed to start with an empty store.
CMD ["python", "-m", "hl7bridge", "serve", "--host", "0.0.0.0", "--seed"]
