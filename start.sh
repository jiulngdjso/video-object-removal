#!/usr/bin/env bash
set -Eeuo pipefail

# 错误处理 trap
trap 'echo "[ERROR] Script failed at line $LINENO with exit code $?" >&2' ERR

PY="/opt/venv/bin/python"
COMFY="/comfyui"
PORT="${COMFYUI_PORT:-8188}"
VOLUME="/runpod-volume"

# 常见坑：环境变量开了 HF_HUB_ENABLE_HF_TRANSFER 但镜像里没装 hf_transfer，会直接 ValueError 崩掉
export HF_HUB_ENABLE_HF_TRANSFER=0

echo "[start] =========================================="
echo "[start] Video Object Removal Endpoint Starting..."
echo "[start] =========================================="

# ========================================
# 0) 软链模型目录（从 /runpod-volume/models 到 /comfyui/models）
# ========================================
MODEL_DIRS=(
    "diffusion_models"
    "text_encoders"
    "vae"
    "florence2"
    "sam2"
    "vitmatte"
    "clip_vision"
    "controlnet"
    "loras"
    "embeddings"
    "upscale_models"
    "ipadapter"
    "unet"
    "llm"
)

if [ -d "${VOLUME}/models" ]; then
    echo "[start] Linking model directories from ${VOLUME}/models..."
    for dir in "${MODEL_DIRS[@]}"; do
        src="${VOLUME}/models/${dir}"
        dst="${COMFY}/models/${dir}"
        if [ -d "${src}" ]; then
            # 如果目标已存在且不是软链，先备份
            if [ -e "${dst}" ] && [ ! -L "${dst}" ]; then
                echo "[start]   Backing up existing ${dst}..."
                mv "${dst}" "${dst}.bak.$(date +%s)" || true
            fi
            # 删除旧软链（如果存在）
            rm -f "${dst}" 2>/dev/null || true
            # 创建新软链
            ln -s "${src}" "${dst}"
            echo "[start]   Linked: ${dir}"
        else
            echo "[start]   Skip (not found): ${dir}"
        fi
    done
else
    echo "[start] WARNING: ${VOLUME}/models not found, using default model paths"
fi

# ========================================
# 1) 应用 Florence2 patch 文件
# ========================================
PATCH_DIR="/app/patches/florence2"
FLORENCE2_MODEL_DIR="${COMFY}/models/florence2"

if [ -d "${PATCH_DIR}" ]; then
    echo "[start] Applying Florence2 patches..."
    mkdir -p "${FLORENCE2_MODEL_DIR}"
    
    # 复制 patch 文件到 florence2 模型目录
    for fn in configuration_florence2.py modeling_florence2.py; do
        if [ -f "${PATCH_DIR}/${fn}" ]; then
            cp "${PATCH_DIR}/${fn}" "${FLORENCE2_MODEL_DIR}/${fn}"
            echo "[start]   Copied: ${fn} -> ${FLORENCE2_MODEL_DIR}/"
        fi
    done
    
    # 同时复制到 DocVQA 子目录（有些节点会用这个结构）
    DOCVQA_DIR="${FLORENCE2_MODEL_DIR}/DocVQA"
    mkdir -p "${DOCVQA_DIR}"
    for fn in configuration_florence2.py modeling_florence2.py; do
        if [ -f "${PATCH_DIR}/${fn}" ]; then
            cp "${PATCH_DIR}/${fn}" "${DOCVQA_DIR}/${fn}"
            echo "[start]   Copied: ${fn} -> ${DOCVQA_DIR}/"
        fi
    done
else
    echo "[start] WARNING: Patch directory not found: ${PATCH_DIR}"
fi

# ========================================
# 2) 启动 ComfyUI（后台）
# ========================================
echo "[start] Launching ComfyUI on port ${PORT}..."
cd "${COMFY}"
nohup "${PY}" -u main.py \
    --listen 127.0.0.1 \
    --port "${PORT}" \
    --disable-auto-launch \
    > /tmp/comfyui.log 2>&1 &

COMFY_PID=$!
echo "[start] ComfyUI PID: ${COMFY_PID}"

# ========================================
# 3) 等待 ComfyUI 就绪
# ========================================
echo "[start] Waiting for ComfyUI to be ready..."
MAX_WAIT=180
for i in $(seq 1 ${MAX_WAIT}); do
    if curl -fsS "http://127.0.0.1:${PORT}/system_stats" >/dev/null 2>&1; then
        echo "[start] ComfyUI is ready! (waited ${i}s)"
        break
    fi
    if [ $i -eq ${MAX_WAIT} ]; then
        echo "[start] ERROR: ComfyUI failed to start within ${MAX_WAIT}s"
        echo "[start] Last 50 lines of ComfyUI log:"
        tail -50 /tmp/comfyui.log || true
        exit 1
    fi
    sleep 1
done

# ========================================
# 4) 启动 RunPod serverless handler（前台）
# ========================================
echo "[start] Launching RunPod handler..."
exec "${PY}" -u /app/handler.py
