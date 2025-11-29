#!/bin/bash
# Setup gVisor (runsc) on Linux
# Run with sudo

set -e

ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    ARCH="amd64"
elif [ "$ARCH" = "aarch64" ]; then
    ARCH="arm64"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

echo "Installing gVisor for $ARCH..."

# Download runsc
wget "https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/runsc" -O /tmp/runsc
wget "https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}/containerd-shim-runsc-v1" -O /tmp/containerd-shim-runsc-v1

# Install binaries
sudo install -m 755 /tmp/runsc /usr/local/bin/
sudo install -m 755 /tmp/containerd-shim-runsc-v1 /usr/local/bin/

# Clean up
rm /tmp/runsc /tmp/containerd-shim-runsc-v1

# Configure Docker daemon
echo "Configuring Docker..."
if [ -f /etc/docker/daemon.json ]; then
    # Backup existing config
    sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak

    # Merge runtimes into existing config
    sudo python3 -c "
import json
with open('/etc/docker/daemon.json', 'r') as f:
    config = json.load(f)
config.setdefault('runtimes', {})
config['runtimes']['runsc'] = {'path': '/usr/local/bin/runsc'}
with open('/etc/docker/daemon.json', 'w') as f:
    json.dump(config, f, indent=2)
"
else
    sudo mkdir -p /etc/docker
    echo '{
  "runtimes": {
    "runsc": {
      "path": "/usr/local/bin/runsc"
    }
  }
}' | sudo tee /etc/docker/daemon.json
fi

# Restart Docker
sudo systemctl restart docker

echo "gVisor installed successfully!"
echo "Test with: docker run --runtime=runsc hello-world"
