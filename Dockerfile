# ============================================
# Video Object Removal - RunPod Serverless Endpoint
# 基于 runpod/worker-comfyui:5.5.1-base
# ============================================

FROM runpod/worker-comfyui:5.5.1-base

USER root

# ============================================
# 1) 安装系统依赖
# ============================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    ca-certificates \
    curl \
 && rm -rf /var/lib/apt/lists/*

# ============================================
# 2) 环境变量
# ============================================
ENV COMFYUI_DIR=/comfyui
ENV COMFYUI_PORT=8188
ENV PYTHONUNBUFFERED=1
ENV HF_HUB_ENABLE_HF_TRANSFER=0

WORKDIR /app

# ============================================
# 3) 安装 Python 依赖（使用锁定版本）
# ============================================
COPY locks/requirements.lock.txt /tmp/requirements.lock.txt
RUN /opt/venv/bin/python -m pip install --no-cache-dir -r /tmp/requirements.lock.txt

# ============================================
# 4) 安装自定义节点（按 commit 锁定版本）
# ============================================
COPY locks/custom_nodes.lock.txt /tmp/custom_nodes.lock.txt
COPY tools/install_custom_nodes.py /app/tools/install_custom_nodes.py

# 移除 ComfyUI-Manager（避免自动更新污染环境）
RUN rm -rf /comfyui/custom_nodes/ComfyUI-Manager \
 && rm -rf /comfyui/user/default/ComfyUI-Manager || true

# 安装自定义节点
RUN /opt/venv/bin/python /app/tools/install_custom_nodes.py \
      --lock /tmp/custom_nodes.lock.txt \
      --dst /comfyui/custom_nodes

# ============================================
# 5) 复制工作流、patches、handler、start.sh
# ============================================
# 工作流
COPY workflows/workflow_api.json /app/workflows/workflow_api.json

# Florence2 patch 文件（关键！）
COPY patches /app/patches

# Handler 和启动脚本
COPY handler.py /app/handler.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# ============================================
# 6) 预创建必要目录
# ============================================
RUN mkdir -p /comfyui/input \
 && mkdir -p /comfyui/output \
 && mkdir -p /comfyui/models/florence2 \
 && mkdir -p /comfyui/models/florence2/DocVQA

# ============================================
# 7) 启动命令
# ============================================
CMD ["/app/start.sh"]
