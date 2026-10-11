FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM public.ecr.aws/docker/library/python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git util-linux ca-certificates libatomic1 libcairo2 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/hermes
RUN git init . && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch --depth=1 origin 7157422022ff06f3e632d1dd394ee1253b17ad37 && git checkout --detach FETCH_HEAD
RUN uv sync --locked --no-dev --extra telegram --extra mcp --python /usr/local/bin/python3.14
# Only this immutable checkout is trusted when running as the owner's UID.
RUN git config --system --add safe.directory /opt/hermes
WORKDIR /opt/emuru
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --python /usr/local/bin/python3.14
COPY src ./src
RUN uv sync --locked --python /usr/local/bin/python3.14
COPY scripts/hermes ./scripts/hermes
COPY infra/hermes ./infra/hermes
COPY infra/docker/secret-exec.py ./infra/docker/secret-exec.py
COPY agents/emuru ./agents/emuru
COPY tests/fixtures/hermes-vault ./tests/fixtures/hermes-vault
COPY tests/telegram/native_session_case.py ./tests/telegram/native_session_case.py
COPY tests/native_inference_case.py ./tests/native_inference_case.py
RUN chmod -R a+rX /opt/emuru/tests/fixtures
RUN chmod a+r \
    /opt/emuru/agents/emuru/SOUL.md \
    /opt/emuru/infra/hermes/telegram-settings.json \
    /opt/emuru/infra/hermes/runtime-lock.json \
    /opt/emuru/infra/hermes/runtime-settings.json \
    /opt/emuru/infra/hermes/telegram-native-contract.json \
    /opt/emuru/infra/hermes/settings.json
RUN mkdir .runtime
ENV EMURU_HERMES_ROOT=/opt/hermes HERMES_DISABLE_LAZY_INSTALLS=1 UV_OFFLINE=1 UV_NO_SYNC=1 PATH=/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin HOME=/state XDG_STATE_HOME=/state
USER 1000:1000
ENTRYPOINT ["python", "/opt/emuru/infra/docker/secret-exec.py"]
CMD ["scripts/hermes/telegram.sh"]
