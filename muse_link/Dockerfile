ARG BUILD_FROM
FROM ${BUILD_FROM}

# Meta's Muse Gadgets SDK (Apache 2.0), pinned to the version this add-on was written against.
ARG MUSE_SDK_COMMIT=4268880f4bf3e9c736c08ea5efeac6f628fe6f97

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

COPY rootfs/ /

CMD ["/opt/venv/bin/python", "/opt/muse-link/muse_link.py"]
