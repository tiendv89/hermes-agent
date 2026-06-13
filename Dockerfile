FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie AS uv_source

FROM debian:13.4

ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates python3 python-is-python3 python3-venv python3-dev gcc git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=uv_source /usr/local/bin/uv /usr/local/bin/uv
RUN ln -s /usr/local/bin/uv /usr/local/bin/uvx

WORKDIR /app

# Parent project metadata + lockfile.
COPY pyproject.toml uv.lock* ./
# Vendored upstream agent (consumed as an editable path dependency). Our code
# imports its top-level modules directly (run_agent, hermes_state, hermes_cli.*),
# so the full submodule tree must be present on the image.
COPY vendor/hermes-agent/ vendor/hermes-agent/
# Workflow code + root-level migrations + agent home.
COPY src/ src/
COPY plugins/ plugins/
COPY migrations/ migrations/
COPY hermes_home/ /root/.hermes/

# Install the vendored agent first (editable, with the anthropic extra), then
# the gateway project. The gateway's hermes-agent[anthropic] requirement is then
# already satisfied by the editable install above.
RUN uv pip install --system --break-system-packages -e "./vendor/hermes-agent[anthropic]" \
 && uv pip install --system --break-system-packages -e ".[workflow-gateway]"

EXPOSE 8000

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]
