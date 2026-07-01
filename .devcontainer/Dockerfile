FROM mcr.microsoft.com/devcontainers/python:3.11

# Install gcloud SDK
RUN curl -sSL https://sdk.cloud.google.com | bash -s -- --install-dir=/usr/local/gcloud --disable-prompts \
    && echo 'export PATH=/usr/local/gcloud/google-cloud-sdk/bin:$PATH' >> /etc/bash.bashrc

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
    
WORKDIR /workspace
