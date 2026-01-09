#!/usr/bin/env bash
set -euo pipefail

COMFY_DIR="/comfyui"
PY="/opt/venv/bin/python"
PORT="${COMFYUI_PORT:-8188}"

echo "[start] comfy=${COMFY_DIR} port=${PORT}"

# 0) 可选：如果你用了 RunPod Cached Models，很多人会挂在 /runpod-volume/models
#    那就把常用模型目录软链进 /comfyui/models（没有就跳过）
if [ -d "/runpod-volume/models" ]; then
  echo "[start] found /runpod-volume/models, symlink into /comfyui/models"
  mkdir -p "${COMFY_DIR}/models"
  for d in checkpoints diffusion_models vae text_encoders clip_vision controlnet loras embeddings upscale_models style_models ipadapter unet sam2 sams llm florence2; do
    if [ -d "/runpod-volume/models/$d" ] && [ ! -e "${COMFY_DIR}/models/$d" ]; then
      ln -s "/runpod-volume/models/$d" "${COMFY_DIR}/models/$d"
    fi
  done
fi

# 1) 应用 Florence2 Patch（你 repo 里 patches/florence2 这两个文件）
#    目标：/comfyui/models/florence2 及其 DocVQA 子目录
PATCH_DIR="/workspace/patches/florence2"
FLO_DIR="${COMFY_DIR}/models/florence2"
if [ -d "$PATCH_DIR" ]; then
  echo "[start] apply florence2 patches"
  mkdir -p "$FLO_DIR" "$FLO_DIR/DocVQA"
  cp -f "$PATCH_DIR/modeling_florence2.py" "$FLO_DIR/modeling_florence2.py"
  cp -f "$PATCH_DIR/configuration_florence2.py" "$FLO_DIR/configuration_florence2.py"
  cp -f "$PATCH_DIR/modeling_florence2.py" "$FLO_DIR/DocVQA/modeling_florence2.py"
  cp -f "$PATCH_DIR/configuration_florence2.py" "$FLO_DIR/DocVQA/configuration_florence2.py"
fi

# 2) 保险：避免 timm 混装导致 RotaryEmbedding 报错（你之前踩过的坑）
#    只要你环境里出现那个报错，就靠这一段救命
echo "[start] ensure timm==1.0.15 (safe pin)"
set +e
pkill -f "main.py" >/dev/null 2>&1 || true
rm -rf /opt/venv/lib/python3.12/site-packages/timm* 2>/dev/null
$PY -m pip install --no-cache-dir --no-deps "timm==1.0.15" >/dev/null 2>&1
set -e

# 3) 启动 ComfyUI（后台）
LOG="/tmp/comfyui.log"
echo "[start] launching comfyui..."
nohup $PY "${COMFY_DIR}/main.py" --listen 0.0.0.0 --port "$PORT" --disable-auto-launch >"$LOG" 2>&1 &

# 4) 等待 ComfyUI 就绪
echo "[start] waiting for comfyui..."
for i in {1..120}; do
  if curl -fsS "http://127.0.0.1:${PORT}/system_stats" >/dev/null 2>&1; then
    echo "[start] comfyui ready."
    break
  fi
  sleep 1
done

# 5) 启动 RunPod handler（前台）
echo "[start] starting handler..."
exec $PY -u /workspace/handler.py
