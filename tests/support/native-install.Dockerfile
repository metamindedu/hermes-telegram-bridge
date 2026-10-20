FROM node@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5 AS node
FROM python@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN python -m pip install --no-cache-dir uv==0.12.18
COPY core.tar bridge.tar bridge.bundle /payload/
RUN mkdir -p /opt/core && tar -xf /payload/core.tar -C /opt/core && git clone --no-checkout /payload/bridge.bundle /opt/bridge && tar -xf /payload/bridge.tar -C /opt/bridge && git -C /opt/bridge remote remove origin && rm -rf /payload
ENV HERMES_DISABLE_LAZY_INSTALLS=1 PYTHONDONTWRITEBYTECODE=1
RUN cd /opt/core && UV_PROJECT_ENVIRONMENT=/opt/core/.venv uv sync --locked --no-default-groups --extra telegram && uv pip install --python /opt/core/.venv/bin/python -r /opt/bridge/desktop-plugins/session-complete-telegram/requirements-test.txt
RUN mkdir -p /opt/bridge/.ci-scratch && chmod 1777 /opt/bridge/.ci-scratch
ARG CORE_REF
ARG CORE_GENERATION
ENV GITHUB_WORKSPACE=/opt/bridge HERMES_TEST_ISOLATION_BASE=/opt/bridge/.ci-scratch HERMES_CORE_ROOT=/opt/core HERMES_CORE_REF=${CORE_REF} HERMES_CORE_GENERATION=${CORE_GENERATION} HERMES_BRIDGE_CONTAINER_BOUNDARY=native-pm-v1
WORKDIR /opt/bridge/desktop-plugins/session-complete-telegram
CMD ["/opt/core/.venv/bin/python", "-I", "-S", "/opt/bridge/tests/support/container_native_install.py"]
