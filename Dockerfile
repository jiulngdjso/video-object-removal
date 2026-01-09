FROM runpod/worker-comfyui:5.5.1-base

SHELL ["/bin/bash", "-lc"]
WORKDIR /workspace

# 复制你的仓库
COPY . /workspace

# 基本环境
RUN chmod +x /workspace/start.sh && \
    /opt/venv/bin/python -m pip install --no-cache-dir -U pip

# 1) 安装 custom_nodes（按 locks/custom_nodes.lock.txt 精确 pin）
RUN /opt/venv/bin/python /workspace/tools/install_custom_nodes.py \
      /workspace/locks/custom_nodes.lock.txt \
      /comfyui/custom_nodes

# 2) 安装每个 custom node 自带 requirements（尽量最小化依赖变更）
RUN set -e; \
    for req in /comfyui/custom_nodes/*/requirements.txt; do \
      if [ -f "$req" ]; then \
        echo "[pip] install $req"; \
        /opt/venv/bin/python -m pip install --no-cache-dir -r "$req" || true; \
      fi; \
    done

# 入口
CMD ["bash", "/workspace/start.sh"]
