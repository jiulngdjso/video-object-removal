FROM runpod/worker-comfyui:5.5.1-base

WORKDIR /workspace
COPY . /workspace

# 基本工具（有些 base 里有，但装上更稳）
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 脚本可执行
RUN chmod +x /workspace/start.sh

# 固定 pip 版本（可选）
RUN /opt/venv/bin/python -m pip install --no-cache-dir -U pip

# 1) 按 lock 安装 custom_nodes（repo + commit）
RUN /opt/venv/bin/python /workspace/tools/install_custom_nodes.py /workspace/locks/custom_nodes.lock.txt /comfyui/custom_nodes

# 2) 安装几个常见节点的 requirements（按需）
RUN set -eux; \
    PY=/opt/venv/bin/python; \
    if [ -f /comfyui/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt ]; then $PY -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt; fi; \
    if [ -f /comfyui/custom_nodes/ComfyUI-Crystools/requirements.txt ]; then $PY -m pip install --no-cache-dir -r /comfyui/custom_nodes/Com
