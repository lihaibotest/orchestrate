ARG EXPECTED_BASE_IMAGE_ID=sha256:b916478e77e245f18bd6968cb8ae54a2a323aa960b03ddd738f8344d7c4ea32d
FROM sha256:b916478e77e245f18bd6968cb8ae54a2a323aa960b03ddd738f8344d7c4ea32d

COPY --chown=10001:10001 backend/ /app/backend/
COPY --chown=10001:10001 migrations/ /app/migrations/
COPY --chown=10001:10001 vendor/python/ /app/vendor/python/

# Seed the named volume with an ownership-preserving file. The base image is
# distroless and has no shell, so this uses COPY instead of a RUN instruction.
COPY --chown=10001:10001 .dockerignore /var/lib/orchestrator/.volume-keep

WORKDIR /app/backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend:/app/vendor/python
ARG EXPECTED_BASE_IMAGE_ID=sha256:b916478e77e245f18bd6968cb8ae54a2a323aa960b03ddd738f8344d7c4ea32d
LABEL org.opencontainers.image.base.id="${EXPECTED_BASE_IMAGE_ID}"
USER 10001:10001
