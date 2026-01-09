import os
import json
import time
import uuid
from pathlib import Path

import runpod
import boto3

try:
    import requests
except Exception:
    requests = None
    import urllib.request


COMFY_PORT = int(os.environ.get("COMFYUI_PORT", "8188"))
COMFY_BASE = os.environ.get("COMFYUI_BASE", f"http://127.0.0.1:{COMFY_PORT}")

WORKFLOW_PATH = Path("/workspace/workflows/workflow_api.json")

COMFY_INPUT_DIR = Path("/comfyui/input")
COMFY_OUTPUT_DIR = Path("/comfyui/output")

# R2/S3 env
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    )


def http_post_json(url: str, payload: dict, timeout=60):
    if requests:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_json(url: str, timeout=60):
    if requests:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_comfy_ready(max_wait=180):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            http_get_json(f"{COMFY_BASE}/system_stats", timeout=5)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("ComfyUI not ready")


def find_first_node_id(prompt: dict, class_type: str) -> str | None:
    for nid, node in prompt.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            return str(nid)
    return None


def patch_workflow(prompt: dict, video_filename: str, object_text: str, out_prefix: str):
    # 1) VHS_LoadVideo
    vid_id = find_first_node_id(prompt, "VHS_LoadVideo")
    if not vid_id:
        raise RuntimeError("VHS_LoadVideo not found in workflow")
    prompt[vid_id]["inputs"]["video"] = video_filename

    # 2) Primitive string multiline [Crystools]
    txt_id = find_first_node_id(prompt, "Primitive string multiline [Crystools]")
    if txt_id:
        prompt[txt_id]["inputs"]["string"] = object_text

    # 3) VHS_VideoCombine
    comb_id = find_first_node_id(prompt, "VHS_VideoCombine")
    if comb_id:
        prompt[comb_id]["inputs"]["filename_prefix"] = out_prefix

    return vid_id, txt_id, comb_id


def queue_prompt(prompt: dict) -> str:
    client_id = str(uuid.uuid4())
    resp = http_post_json(f"{COMFY_BASE}/prompt", {"prompt": prompt, "client_id": client_id}, timeout=120)
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"Bad /prompt response: {resp}")
    return pid


def wait_history(prompt_id: str, timeout_sec=3600):
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        hist = http_get_json(f"{COMFY_BASE}/history/{prompt_id}", timeout=60)
        item = hist.get(prompt_id)
        if item and item.get("status", {}).get("completed"):
            return item
        time.sleep(1)
    raise RuntimeError("Timed out waiting for ComfyUI result")


def pick_video_from_history(history_item: dict, combine_node_id: str | None):
    outputs = history_item.get("outputs", {}) or {}

    # 优先从 VHS_VideoCombine 节点取
    if combine_node_id and combine_node_id in outputs:
        o = outputs[combine_node_id]
        for key in ("videos", "gifs", "images"):
            arr = o.get(key)
            if isinstance(arr, list):
                for it in arr:
                    fn = it.get("filename")
                    sub = it.get("subfolder", "")
                    typ = it.get("type", "output")
                    if fn and fn.lower().endswith((".mp4", ".mov", ".webm", ".gif")):
                        base = "/comfyui/output" if typ == "output" else ("/comfyui/temp" if typ == "temp" else "/comfyui/input")
                        return str(Path(base) / sub / fn)

    # fallback：找 output 目录最新 mp4
    vids = sorted(COMFY_OUTPUT_DIR.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if vids:
        return str(vids[0])

    raise RuntimeError("No output video found")


def handler(job):
    wait_comfy_ready()

    inp = job.get("input", {}) or {}
    params = inp.get("params", {}) or {}

    input_key = inp.get("input_key")
    if not input_key:
        raise ValueError("missing input_key")

    job_id = inp.get("job_id") or job.get("id") or uuid.uuid4().hex[:16]
    object_text = params.get("object") or params.get("object_text") or params.get("prompt") or "object"
    output_key = inp.get("output_key") or f"outputs/{job_id}.mp4"

    # 下载到 /comfyui/input
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    local_in = COMFY_INPUT_DIR / f"{job_id}_{Path(input_key).name}"

    s3 = s3_client()
    s3.download_file(S3_BUCKET, input_key, str(local_in))

    # load workflow & patch
    wf = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    out_prefix = f"objrm_{job_id}"
    _, _, comb_id = patch_workflow(wf, local_in.name, object_text, out_prefix)

    # run
    prompt_id = queue_prompt(wf)
    hist_item = wait_history(prompt_id, timeout_sec=int(params.get("timeout_sec", 3600)))

    # pick output
    out_path = pick_video_from_history(hist_item, comb_id)
    out_path = Path(out_path)
    if not out_path.exists():
        raise RuntimeError(f"Output file not found: {out_path}")

    # upload
    s3.upload_file(str(out_path), S3_BUCKET, output_key, ExtraArgs={"ContentType": "video/mp4"})

    return {
        "job_id": job_id,
        "input_key": input_key,
        "output_key": output_key,
        "object": object_text,
        "prompt_id": prompt_id,
    }


runpod.serverless.start({"handler": handler})
