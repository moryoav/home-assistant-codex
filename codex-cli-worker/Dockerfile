ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

ARG BUILD_VERSION=dev
ARG BUILD_ARCH=amd64
ARG CODEX_VERSION=0.160.0

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
    bash \
    bubblewrap \
    ca-certificates \
    chromium \
    font-noto \
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
COPY codex-bwrap-probe.py /opt/codex-sandbox/codex-bwrap-probe.py

RUN chmod 0755 /opt/codex-sandbox/bwrap

ENV PATH="/opt/codex-sandbox:${PATH}"

RUN npm install -g --prefix /usr/local "@openai/codex@${CODEX_VERSION}" \
    && test -x /usr/local/bin/codex \
    && test "$(command -v codex)" = /usr/local/bin/codex \
    && /usr/local/bin/codex --version

# Alpine packages Chromium for both supported architectures. Use its browser,
# rather than downloading a glibc-only Playwright build into the image.
RUN npm install --prefix /opt/verification --omit=dev playwright-core@1.63.0
ENV NODE_PATH=/opt/verification/node_modules

RUN pip3 install --no-cache-dir --break-system-packages \
    flask \
    pyyaml \
    qrcode \
    requests \
    websocket-client

WORKDIR /data

COPY run.sh /
COPY server.py /
COPY verification.py verification_mcp.py browser.cjs browser_resources.cjs /
COPY web /web

RUN chmod a+x /run.sh /server.py

CMD ["/run.sh"]
