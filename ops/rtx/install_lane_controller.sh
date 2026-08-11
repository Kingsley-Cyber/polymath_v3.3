#!/usr/bin/env bash
# Idempotent install of the Polymath vLLM lane controller on the RTX box (WSL).
# Run ON the box:  bash install_lane_controller.sh
# Prereqs: /home/kingsley/vllm-server/controller-key.txt (the lane API key,
# installed over SSH — never through chat), sudo NOPASSWD for systemctl.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$DIR/lane_controller.py" /home/kingsley/vllm-server/lane_controller.py
test -s /home/kingsley/vllm-server/controller-key.txt || { echo "FATAL: controller-key.txt missing"; exit 1; }
sudo install -m 0644 "$DIR/vllm-lane-controller.service" /etc/systemd/system/vllm-lane-controller.service
sudo systemctl daemon-reload
sudo systemctl enable --now vllm-lane-controller
sleep 2
systemctl show vllm-lane-controller -p ActiveState
