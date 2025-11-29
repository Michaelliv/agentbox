FROM python:3.13-slim-bookworm

# Install common unix utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    coreutils \
    grep \
    sed \
    gawk \
    findutils \
    curl \
    wget \
    git \
    jq \
    tree \
    vim-tiny \
    less \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for process_api
RUN pip install --no-cache-dir aiohttp

# Create directory structure matching the mount pattern
RUN mkdir -p /mnt/user-data/uploads \
    && mkdir -p /mnt/user-data/outputs \
    && mkdir -p /workspace

# Copy and setup the init process
COPY process_api.py /usr/local/bin/process_api
RUN chmod +x /usr/local/bin/process_api

# Set workspace as default working directory
WORKDIR /workspace

# Expose the process_api port
EXPOSE 2024

# Run process_api as PID 1
# gVisor provides the security boundary, so root inside sandbox is safe
CMD ["/usr/local/bin/process_api", "--addr", "0.0.0.0:2024", "--memory-limit-bytes", "4294967296"]
