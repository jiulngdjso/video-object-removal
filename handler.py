import os
import json
import time
from pathlib import Path

import requests
import boto3
import runpod


COMFY_URL = f"http://127.0.0.1:{os.environ.get('COMFYUI_PORT', '8188')}"
WORKFLOW_PATH = Path("/app/workflows/workflow_api.json")
PATCH_DIR = Path("/app/patches/florence2")

# R2/S3 env
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")
S3_BUCKET = os.environ.get("S3_BUCKET")

COMFY_INPUT_DIR = Path("/comfyui/input")
COMFY_OUTPUT_DIR = Path("/comfyui/output")
FLORENCE2_DIR = Path("/comfyui/models/florence2")


def s3_client():
    if not (S3_ENDPOINT_URL and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY):
        raise RuntimeError("Missing S3 env vars: S3_ENDPOINT_URL/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        region_name=os.environ.get("S3_REGION", "auto"),
    )


def apply_florence2_patch():
    # 覆盖/写入 patch 文件（避免下载的原版不兼容）
    FLORENCE2_DIR.mkdir(parents=True, exist_ok=True)
    if PATCH_DIR.exists():
        for fn in ["configuration_florence2.py", "modeling_florence2.py"]:
            src = PATCH_DIR / fn
            if src.exists():
                (FLORENCE2_DIR / fn).write_bytes(src.read_bytes())
        # 也给 DocVQA 子目录准备一份（有些节点会用这个结构）
        docvqa = FLORENCE2_DIR / "DocVQA"
        docvqa.mkdir(parents=True, exist_ok=True)
        for fn in ["configuration_florence2.py", "modeling_florence2.py"]:
            src = PATCH_DIR / fn
            if src.exists():
                (docvqa / fn).write_bytes(src.read_bytes())


def wait_comfy_ready(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{COMFY_URL}/system_stats", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("ComfyUI not ready / timeout")


def load_workflow():
    if not WORKFLOW_PATH.exists():
        raise RuntimeError(f"workflow not found: {WORKFLOW_PATH}")
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def submit_workflow(prompt: dict):
    r = requests.post(f"{COMFY_URL}/prompt", json={"prompt": prompt}, timeout=30)
    r.raise_for_status()
    return r.json()["prompt_id"]


def wait_result(prompt_id: str, timeout=3600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=30)
        r.raise_for_status()
        hist = r.json()
        if prompt_id in hist:
            return hist[prompt_id]
        time.sleep(1)
    raise RuntimeError("timeout waiting history")


def pick_output_mp4(history_item: dict) -> Path:
    """
    在 ComfyUI history 输出里找 mp4 文件名，然后拼到 /comfyui/output
    """
    outputs = history_item.get("outputs", {}) or {}
    # 先找明确的 files
    cand = []
    for _node_id, out in outputs.items():
        if isinstance(out, dict):
            # VHS 常见字段："gifs" / "videos" / "files"
            for key in ("videos", "files", "gifs"):
                if key in out and isinstance(out[key], list):
                    for it in out[key]:
                        if isinstance(it, dict) and "filename" in it:
                            fn = it["filename"]
                            if str(fn).lower().endswith(".mp4"):
                                cand.append(fn)
    if cand:
        return COMFY_OUTPUT_DIR / cand[-1]

    # 兜底：直接在 output 目录里挑最新 mp4
    mp4s = sorted(COMFY_OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not mp4s:
        raise RuntimeError("No mp4 output found in history or output folder")
    return mp4s[-1]


def handler(event):
    """
    RunPod input:
    {
      "input": {
        "job_id": "test001",
        "input_key": "inputs/test001.mp4",
        "output_key": "outputs/test001_out.mp4",
        "remove_text": "necklace"
      }
    }
    """
    inp = event.get("input") or {}
    job_id = inp.get("job_id") or f"job_{int(time.time())}"
    input_key = inp.get("input_key")
    output_key = inp.get("output_key") or f"outputs/{job_id}.mp4"
    remove_text = (inp.get("remove_text") or "").strip() or "object"

    if not input_key:
        return {"error": "missing input_key"}

    if not S3_BUCKET:
        return {"error": "missing S3_BUCKET"}

    wait_comfy_ready()
    apply_florence2_patch()

    # 下载输入视频 -> /comfyui/input
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    local_in = COMFY_INPUT_DIR / f"{job_id}.mp4"

    s3 = s3_client()
    s3.download_file(S3_BUCKET, input_key, str(local_in))

    # 组装 workflow（替换两个占位符）
    wf = load_workflow()

    # 25: VHS_LoadVideo -> inputs.video 填“文件名”（不是key）
    if "25" in wf and "inputs" in wf["25"]:
        wf["25"]["inputs"]["video"] = local_in.name

    # 30: Primitive string multiline -> inputs.string
    if "30" in wf and "inputs" in wf["30"]:
        wf["30"]["inputs"]["string"] = remove_text

    # 13: VHS_VideoCombine -> filename_prefix
    if "13" in wf and "inputs" in wf["13"]:
        wf["13"]["inputs"]["filename_prefix"] = f"{job_id}_out"

    prompt_id = submit_workflow(wf)
    hist_item = wait_result(prompt_id)

    out_path = pick_output_mp4(hist_item)
    if not out_path.exists():
        raise RuntimeError(f"output file not found: {out_path}")

    # 上传输出
    s3.upload_file(str(out_path), S3_BUCKET, output_key)

    return {
        "job_id": job_id,
        "input_key": input_key,
        "output_key": output_key,
        "remove_text": remove_text,
        "output_filename": out_path.name,
    }


runpod.serverless.start({"handler": handler})
