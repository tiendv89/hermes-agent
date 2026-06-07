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

# Install the package with workflow-gateway extra (asyncpg + anthropic)
COPY pyproject.toml uv.lock* ./
COPY run_agent.py hermes_constants.py hermes_logging.py hermes_state.py \
     hermes_time.py hermes_bootstrap.py utils.py model_tools.py toolsets.py \
     toolset_distributions.py trajectory_compressor.py ./

COPY agent/ agent/
COPY hermes_cli/ hermes_cli/
COPY tools/ tools/
COPY plugins/ plugins/
COPY providers/ providers/
COPY workflow_gateway/ workflow_gateway/
COPY workflow_plugin/ workflow_plugin/
COPY hermes_home/ /root/.hermes/

RUN uv pip install --system --break-system-packages -e ".[workflow-gateway]"

EXPOSE 8000

CMD ["uvicorn", "workflow_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
