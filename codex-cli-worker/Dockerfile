ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64
ARG CODEX_VERSION=0.146.0

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
    bash \
    bubblewrap \
    ca-certificates \
    coreutils \
    git \
    jq \
    nodejs \
    npm \
    python3 \
    py3-pip \
    rsync \
    tar

RUN install -D -m 0755 /usr/bin/bwrap /opt/codex-sandbox/bwrap.real

COPY codex-bwrap /opt/codex-sandbox/bwrap

RUN chmod 0755 /opt/codex-sandbox/bwrap

ENV PATH="/opt/codex-sandbox:${PATH}"

RUN npm install -g --prefix /usr/local "@openai/codex@${CODEX_VERSION}" \
    && test -x /usr/local/bin/codex \
    && test "$(command -v codex)" = /usr/local/bin/codex \
    && /usr/local/bin/codex --version

RUN pip3 install --no-cache-dir --break-system-packages \
    flask \
    pyyaml \
    qrcode \
    requests \
    websocket-client

WORKDIR /data

COPY run.sh /
COPY server.py /

RUN chmod a+x /run.sh /server.py

CMD ["/run.sh"]
