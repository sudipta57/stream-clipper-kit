#!/usr/bin/env bash
# Lets Docker containers use the NVIDIA GPU (needed by OpenShorts' GPU image).
# Source: NVIDIA's official install guide for the NVIDIA Container Toolkit.
# What it changes on this computer:
#   1. adds NVIDIA's package source (/etc/apt/sources.list.d/nvidia-container-toolkit.list)
#   2. installs the nvidia-container-toolkit package
#   3. registers the "nvidia" runtime in /etc/docker/daemon.json and restarts Docker
set -euo pipefail

echo "==> Adding NVIDIA's package signing key and source"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

echo "==> Installing nvidia-container-toolkit"
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

echo "==> Registering the NVIDIA runtime with Docker and restarting Docker"
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

echo "==> Done. Docker runtimes now:"
docker info 2>/dev/null | grep -i runtimes
