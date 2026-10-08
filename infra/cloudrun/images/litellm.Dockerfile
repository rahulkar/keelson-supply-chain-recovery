# syntax=docker/dockerfile:1.7
# LiteLLM for Cloud Run: infra/litellm/config.yaml baked in (compose bind-mounts it). The config
# holds no secrets (os.environ/... references only). Build context: REPOSITORY ROOT.
#   docker build -f infra/cloudrun/images/litellm.Dockerfile .
ARG LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.104.0
FROM ${LITELLM_IMAGE}
COPY infra/litellm/config.yaml /app/config.yaml
