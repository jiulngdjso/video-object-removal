#!/usr/bin/env bash
set -euo pipefail

PY="/opt/venv/bin/python"
COMFY="/comfyui"
PORT="${COMFYUI_PORT:-8188}"

# 常见坑：环境变量开了 HF_HUB_ENABLE_HF_TRANSFER 但镜像里没装 hf_transfer，会直接 ValueError 崩掉
export HF_HUB_ENABLE_HF_TRANSFER=0

# 启动 ComfyUI（后台）
echo "[start] launching ComfyUI..."
nohup "${PY}" -u "${COMFY}/main.py" --listen 0.0.0.0 --port "${PORT}" \
  > /tmp/comfyui.log 2>&1 &

# 等 ComfyUI 就绪
echo "[start] waiting ComfyUI on :${PORT} ..."
for i in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:${PORT}/system_stats" >/dev/null 2>&1; then
    echo "[start] ComfyUI is ready."
    break
  fi
  sleep 1
done

# 前台跑 RunPod serverless handler
echo "[start] launching handler..."
exec "${PY}" -u /app/handler.py
