# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# LiteLLM for Cloud Run: infra/litellm/config.yaml baked in (compose bind-mounts it). The config
# holds no secrets (os.environ/... references only). Build context: REPOSITORY ROOT.
#   docker build -f infra/cloudrun/images/litellm.Dockerfile .
ARG LITELLM_IMAGE=ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e
FROM ${LITELLM_IMAGE}
COPY infra/litellm/config.yaml /app/config.yaml
