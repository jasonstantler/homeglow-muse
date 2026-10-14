ARG BUILD_FROM
FROM ${BUILD_FROM}

# Meta's Muse Gadgets SDK (Apache 2.0), pinned to the version this add-on was written against.
ARG MUSE_SDK_COMMIT=4268880f4bf3e9c736c08ea5efeac6f628fe6f97
# Google's Bumble Bluetooth stack (Apache 2.0), v0.0.235, for pairing with the Muse app.
ARG BUMBLE_COMMIT=745d607029d5c2607659894daf4139736aa6e2ff

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dbus python3-gi python3-cryptography ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/sdk \
    && curl -fsSL "https://github.com/facebookincubator/muse-gadget-sdk/archive/${MUSE_SDK_COMMIT}.tar.gz" \
        | tar -xz -C /opt/sdk --strip-components=1 \
    && python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/sdk/linux/src/musegadget/data/requirements.lock \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps /opt/sdk/linux \
    && cp /opt/sdk/LICENSE /opt/MUSE-SDK-LICENSE \
    && rm -rf /opt/sdk

# Bumble's core needs only cryptography (from Debian, above), pyee and a newer typing_extensions.
RUN mkdir -p /opt/bumble-src /opt/bumble \
    && curl -fsSL "https://github.com/google/bumble/archive/${BUMBLE_COMMIT}.tar.gz" \
        | tar -xz -C /opt/bumble-src --strip-components=1 \
    && mv /opt/bumble-src/bumble /opt/bumble/bumble \
    && cp /opt/bumble-src/LICENSE /opt/BUMBLE-LICENSE \
    && rm -rf /opt/bumble-src \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps "pyee==13.0.0" "typing_extensions==4.12.2" \
    && /opt/venv/bin/python -c "import site; open(site.getsitepackages()[0] + '/bumble.pth', 'w').write('/opt/bumble\n')" \
    && /opt/venv/bin/python -c "import bumble.device, bumble.transport.hci_socket"

COPY rootfs/ /

CMD ["/opt/venv/bin/python", "/opt/muse-link/muse_link.py"]
