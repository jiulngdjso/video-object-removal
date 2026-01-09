FROM runpod/worker-comfyui:5.5.1-base

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

ENV COMFYUI_DIR=/comfyui
ENV COMFYUI_PORT=8188
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Python 依赖（你导出的 requirements.lock.txt）
COPY locks/requirements.lock.txt /tmp/requirements.lock.txt
RUN /opt/venv/bin/python -m pip install --no-cache-dir -r /tmp/requirements.lock.txt

# 2) 安装 custom_nodes（按 repo|commit 锁定）
COPY locks/custom_nodes.lock.txt /tmp/custom_nodes.lock.txt
COPY tools/install_custom_nodes.py /app/tools/install_custom_nodes.py

# 可选：移除 Manager（避免 Manager 自动更新/污染）
RUN rm -rf /comfyui/custom_nodes/ComfyUI-Manager \
 && rm -rf /comfyui/user/default/ComfyUI-Manager || true

RUN /opt/venv/bin/python /app/tools/install_custom_nodes.py \
      --lock /tmp/custom_nodes.lock.txt \
      --dst /comfyui/custom_nodes

# 3) 复制工作流、patch、handler、start
COPY workflows/workflow_api.json /app/workflows/workflow_api.json
COPY patches /app/patches
COPY handler.py /app/handler.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
