# Video Object Removal - RunPod Serverless Endpoint

基于 ComfyUI + WanVideo MiniMax Remover 的视频物体移除服务。

## 功能特性

- 使用 Florence2 + SAM2 自动检测并分割目标物体
- 使用 WanVideo MiniMax Remover 进行 AI 修复
- 支持自定义移除对象（通过文本描述）
- 自动视频预处理（降帧率、裁剪）
- 与 Poofpop API 完全兼容

## 目录结构

```
video-object-removal/
├── Dockerfile                    # 容器构建配置
├── start.sh                      # 启动脚本（ComfyUI + Handler）
├── handler.py                    # RunPod Serverless Handler
├── workflows/
│   └── workflow_api.json         # ComfyUI 工作流
├── locks/
│   ├── requirements.lock.txt     # Python 依赖锁定
│   └── custom_nodes.lock.txt     # 自定义节点锁定
├── tools/
│   └── install_custom_nodes.py   # 节点安装脚本
├── patches/
│   └── florence2/                # Florence2 patch 文件
│       ├── configuration_florence2.py
│       └── modeling_florence2.py
└── README.md
```

## 构建镜像

```bash
# 克隆仓库
git clone https://github.com/jiulngdjso/video-object-removal.git
cd video-object-removal

# 构建镜像
docker build -t your-registry/video-object-removal:latest .

# 推送到镜像仓库
docker push your-registry/video-object-removal:latest
```

## RunPod Endpoint 配置

### 1. 创建 Serverless Endpoint

在 [RunPod Console](https://www.runpod.io/console/serverless) 创建新的 Serverless Endpoint：

| 配置项 | 推荐值 |
|--------|--------|
| Container Image | your-registry/video-object-removal:latest |
| GPU | RTX 4090 或 A100（推荐 24GB+ VRAM） |
| Min Workers | 0 |
| Max Workers | 根据预算设置 |
| Idle Timeout | 60 秒 |
| Flash Boot | 开启（推荐） |

### 2. 配置环境变量

在 Endpoint 设置中添加以下环境变量：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `S3_ENDPOINT_URL` | R2/S3 端点 URL | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `S3_BUCKET` | 存储桶名称 | `poofpop-media` |
| `S3_ACCESS_KEY_ID` | R2 Access Key | `xxx` |
| `S3_SECRET_ACCESS_KEY` | R2 Secret Key | `xxx` |

### 3. 配置 Network Volume（模型存储）

创建 Network Volume 并挂载到 `/runpod-volume`，目录结构：

```
/runpod-volume/models/
├── diffusion_models/
│   └── Wan2_1-MiniMaxRemover_1_3B_fp16.safetensors
├── text_encoders/
│   └── umt5_xxl_fp16.safetensors
├── vae/
│   └── Wan2_1_VAE_bf16.safetensors
├── florence2/
│   └── DocVQA/
│       ├── config.json
│       ├── model.safetensors
│       └── ...
├── sam2/
│   └── sam2.1_hiera_base_plus.safetensors
└── vitmatte/
    └── ...
```

**模型下载链接**：
- WanVideo 模型: [Hugging Face](https://huggingface.co/Kijai/WanVideo_comfy)
- SAM2 模型: [Hugging Face](https://huggingface.co/facebook/sam2.1-hiera-base-plus)
- Florence2 DocVQA: [Hugging Face](https://huggingface.co/microsoft/Florence-2-base-ft)

## API 使用

### 输入格式

```json
{
  "input": {
    "job_id": "uuid-xxx",
    "input_key": "inputs/video-object-removal/uuid-xxx/video.mp4",
    "output_key": "outputs/video-object-removal/uuid-xxx.mp4",
    "params": {
      "remove_text": "person",
      "timeout_sec": 3600
    }
  }
}
```

### 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `job_id` | string | 否 | 自动生成 | 任务 ID |
| `input_key` | string | 是 | - | R2 中的输入视频路径 |
| `output_key` | string | 否 | 自动生成 | R2 中的输出视频路径 |
| `params.remove_text` | string | 否 | "object" | 要移除的对象描述 |
| `params.timeout_sec` | int | 否 | 3600 | 超时时间（秒） |

### 输出格式

```json
{
  "job_id": "uuid-xxx",
  "input_key": "inputs/video-object-removal/uuid-xxx/video.mp4",
  "output_key": "outputs/video-object-removal/uuid-xxx.mp4",
  "remove_text": "person",
  "prompt_id": "comfyui-prompt-id",
  "preprocess": {
    "fps_in": 30,
    "dur_in": 10.5,
    "fps_cap": 30,
    "max_seconds": 15,
    "downsampled": false,
    "trimmed": false,
    "transcoded": false,
    "remuxed": false,
    "path": "/tmp/jobs/xxx/in.mp4"
  },
  "timing_sec": {
    "download": 1.234,
    "preprocess": 0.567,
    "workflow_prepare": 0.123,
    "comfy_run": 45.678,
    "upload": 2.345,
    "total": 49.947
  },
  "comfy_status": {}
}
```

## 与 Poofpop API 集成

### 1. 更新 ENDPOINTS_JSON

```bash
wrangler secret put ENDPOINTS_JSON
# 输入: {"minimax_remove":"ep-xxx","video-object-removal":"ep-yyy"}
```

### 2. 完整调用流程

```bash
export WORKER_URL="https://poofpop-api.xxx.workers.dev"

# 1. 初始化上传
curl -X POST "$WORKER_URL/upload-init" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "video-object-removal",
    "file_name": "test.mp4",
    "content_type": "video/mp4"
  }'

# 2. 上传视频到 R2
curl -X PUT "<upload_url>" \
  -H "Content-Type: video/mp4" \
  --data-binary @test.mp4

# 3. 创建处理任务
curl -X POST "$WORKER_URL/process" \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "video-object-removal",
    "file_id": "<file_id>",
    "input_key": "<input_key>",
    "params": {
      "remove_text": "person"
    }
  }'

# 4. 查询任务状态
curl "$WORKER_URL/jobs/<job_id>"

# 5. 获取下载链接
curl "$WORKER_URL/download/<job_id>"

# 6. 下载结果
curl -o output.mp4 "<download_url>"
```

## 本地测试

### 使用 Docker 本地运行

```bash
# 构建镜像
docker build -t video-object-removal:local .

# 运行容器（需要 GPU）
docker run --gpus all \
  -p 8188:8188 \
  -e S3_ENDPOINT_URL="https://xxx.r2.cloudflarestorage.com" \
  -e S3_BUCKET="poofpop-media" \
  -e S3_ACCESS_KEY_ID="xxx" \
  -e S3_SECRET_ACCESS_KEY="xxx" \
  -v /path/to/models:/runpod-volume/models \
  video-object-removal:local
```

### 直接调用 RunPod API

```bash
export RUNPOD_API_KEY="your-api-key"
export ENDPOINT_ID="your-endpoint-id"

curl -X POST "https://api.runpod.ai/v2/${ENDPOINT_ID}/run" \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "job_id": "test-001",
      "input_key": "inputs/video-object-removal/test/video.mp4",
      "output_key": "outputs/video-object-removal/test-001.mp4",
      "params": {
        "remove_text": "person"
      }
    }
  }'
```

## 视频预处理策略

Handler 会自动对输入视频进行预处理：

| 条件 | 处理方式 |
|------|----------|
| fps > 30 | 降帧率到 30fps（重编码） |
| 时长 > 15秒 | 裁剪到前 15 秒 |
| 只需裁剪 | Stream copy（不重编码） |
| 都不需要 | 直接使用原文件 |

## Florence2 Patch 说明

`patches/florence2/` 目录包含两个 patch 文件：
- `configuration_florence2.py`
- `modeling_florence2.py`

这些文件会在启动时自动复制到：
- `/comfyui/models/florence2/`
- `/comfyui/models/florence2/DocVQA/`

确保 Florence2 模型能正确加载和运行。

## 故障排查

### ComfyUI 启动失败

```bash
# 查看 ComfyUI 日志
docker exec <container_id> cat /tmp/comfyui.log
```

### 模型加载失败

1. 检查 Network Volume 是否正确挂载
2. 检查模型文件是否存在于正确路径
3. 检查模型文件名是否与 workflow 中一致

### Florence2 相关错误

确保 patch 文件已正确复制到模型目录：
```bash
ls -la /comfyui/models/florence2/
ls -la /comfyui/models/florence2/DocVQA/
```

## 自定义节点列表

| 节点包 | 用途 |
|--------|------|
| ComfyUI-VideoHelperSuite | 视频加载/合成 |
| ComfyUI-WanVideoWrapper | WanVideo 编码/解码/采样 |
| ComfyUI_LayerStyle | Florence2、SAM2、图层工具 |
| ComfyUI_LayerStyle_Advance | 高级图层功能 |
| ComfyUI-KJNodes | 遮罩处理 |
| ComfyUI-Crystools | 基础工具节点 |
| ComfyUI-Easy-Use | 便捷工具节点 |

## License

MIT
