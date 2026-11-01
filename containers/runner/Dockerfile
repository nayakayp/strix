FROM ubuntu:24.04

# This is the runner host, not the Kali tool container in containers/Dockerfile.
# Provider keys, model credentials, source trees, and run data are never baked in.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl docker.io git iptables python3 python3-venv sudo && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/runner-venv && \
    /opt/runner-venv/bin/pip install --no-cache-dir uv==0.9.26 && \
    ln -s /opt/runner-venv/bin/uv /usr/local/bin/uv

RUN useradd --create-home --shell /bin/bash --groups docker runner && \
    echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner && \
    chmod 0440 /etc/sudoers.d/runner && \
    mkdir /workspace && chown runner:runner /workspace

COPY start.sh /usr/local/bin/natsuhat-runner-start
RUN chmod 0755 /usr/local/bin/natsuhat-runner-start

USER runner
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/natsuhat-runner-start"]
