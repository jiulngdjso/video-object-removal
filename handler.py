#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Object Removal Handler for RunPod Serverless

完整闭环：R2 下载 -> ffmpeg 预处理 -> ComfyUI 工作流 -> R2 上传

入参格式（对齐 poofpop-api Worker）：
{
    "input": {
        "job_id": "uuid-xxx",
        "input_key": "inputs/video-object-removal/uuid-xxx/video.mp4",
        "output_key": "outputs/video-object-removal/uuid-xxx.mp4",
        "params": {
            "remove_text": "person",      # 要移除的对象描述
            "timeout_sec": 3600           # 可选，超时时间
        }
    }
}

输出格式：
{
    "job_id": "uuid-xxx",
    "input_key": "...",
    "output_key": "...",
    "remove_text": "person",
    "preprocess": {...},
    "timing_sec": {...}
}
"""

import os
import json
import time
import uuid
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

import boto3
import requests
import runpod


# ========================================
# 配置常量
# ========================================
COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFYUI_PORT") or os.environ.get("COMFY_PORT") or "8188")
COMFY_BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

# R2/S3 环境变量
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")

# 视频预处理策略（对齐 qushuiying）
FPS_CAP = 30          # fps > 30 才降到 30
MAX_SECONDS = 15      # 时长 > 15 秒才裁剪

# 路径常量
WORKFLOW_PATH = Path("/app/workflows/workflow_api.json")
PATCH_DIR = Path("/app/patches/florence2")
COMFY_INPUT_DIR = Path("/comfyui/input")
COMFY_OUTPUT_DIR = Path("/comfyui/output")
FLORENCE2_MODEL_DIR = Path("/comfyui/models/florence2")


# ========================================
# 辅助函数
# ========================================
def _require_env(name: str, val: Optional[str]) -> None:
    if not val:
        raise RuntimeError(f"Missing env var: {name}")


def sh(cmd: list, check: bool = True) -> str:
    """执行 shell 命令"""
    print(f"[sh] {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\nSTDERR:\n{p.stderr}")
    return p.stdout.strip()


def probe_video(path: str) -> Tuple[Optional[float], Optional[float]]:
    """使用 ffprobe 获取视频 fps 和时长"""
    out = sh([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        path
    ])
    try:
        data = json.loads(out)
        
        fps = None
        stream = (data.get("streams") or [{}])[0]
        afr = stream.get("avg_frame_rate") or "0/0"
        rfr = stream.get("r_frame_rate") or "0/0"
        for frac in (afr, rfr):
            try:
                f = Fraction(frac)
                if f.numerator and f.denominator:
                    fps = float(f)
                    break
            except Exception:
                pass
        
        dur = None
        fmt = data.get("format") or {}
        if fmt.get("duration") is not None:
            try:
                dur = float(fmt["duration"])
            except Exception:
                dur = None
        
        return fps, dur
    except Exception:
        return None, None


def ffmpeg_trim_copy(src: str, dst: str, max_seconds: int) -> Dict[str, Any]:
    """裁剪视频（stream copy，不重编码）"""
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-t", str(int(max_seconds)),
        "-c", "copy",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        dst
    ]
    sh(cmd, check=True)
    fps_out, dur_out = probe_video(dst)
    return {"path": dst, "fps_out": fps_out, "dur_out": dur_out, "transcoded": False, "remuxed": True}


def ffmpeg_downsample_encode(src: str, dst: str, fps_cap: int, max_seconds: Optional[int]) -> Dict[str, Any]:
    """降帧率（需要重编码），可选裁剪"""
    base = ["ffmpeg", "-y", "-i", src]
    if max_seconds and max_seconds > 0:
        base += ["-t", str(int(max_seconds))]
    
    base += ["-vf", f"fps={int(fps_cap)}"]
    
    # 先尝试 audio copy
    cmd1 = base + [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        dst
    ]
    
    try:
        sh(cmd1, check=True)
    except Exception:
        # fallback: 重编码音频
        cmd2 = base + [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            dst
        ]
        sh(cmd2, check=True)
    
    fps_out, dur_out = probe_video(dst)
    return {"path": dst, "fps_out": fps_out, "dur_out": dur_out, "transcoded": True, "remuxed": False}


def preprocess_video(src: str, dst: str, fps_cap: int, max_seconds: int) -> Dict[str, Any]:
    """
    视频预处理策略：
    - fps > FPS_CAP -> 降帧率（重编码）
    - 时长 > MAX_SECONDS -> 裁剪
    - 只需裁剪 -> stream copy（不重编码）
    - 都不需要 -> 直接使用原文件
    """
    fps_in, dur_in = probe_video(src)
    
    need_fps = (fps_in is not None and fps_in > (fps_cap + 0.01))
    need_trim = (dur_in is not None and dur_in > (max_seconds + 0.001))
    
    info = {
        "fps_in": fps_in,
        "dur_in": dur_in,
        "fps_cap": fps_cap,
        "max_seconds": max_seconds,
        "downsampled": False,
        "trimmed": False,
        "transcoded": False,
        "remuxed": False,
        "path": src
    }
    
    if not need_fps and not need_trim:
        return info
    
    # 只需裁剪 -> stream copy
    if need_trim and not need_fps:
        out = ffmpeg_trim_copy(src, dst, max_seconds=max_seconds)
        info.update(out)
        info["trimmed"] = True
        return info
    
    # 需要降帧率 -> 重编码（同时裁剪如果需要）
    out = ffmpeg_downsample_encode(src, dst, fps_cap=fps_cap, max_seconds=(max_seconds if need_trim else None))
    info.update(out)
    info["downsampled"] = True
    info["transcoded"] = True
    if need_trim:
        info["trimmed"] = True
    return info


def apply_florence2_patch() -> None:
    """应用 Florence2 patch 文件"""
    FLORENCE2_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if PATCH_DIR.exists():
        for fn in ["configuration_florence2.py", "modeling_florence2.py"]:
            src = PATCH_DIR / fn
            if src.exists():
                (FLORENCE2_MODEL_DIR / fn).write_bytes(src.read_bytes())
                print(f"[patch] Applied: {fn}", flush=True)
        
        # 同时复制到 DocVQA 子目录
        docvqa = FLORENCE2_MODEL_DIR / "DocVQA"
        docvqa.mkdir(parents=True, exist_ok=True)
        for fn in ["configuration_florence2.py", "modeling_florence2.py"]:
            src = PATCH_DIR / fn
            if src.exists():
                (docvqa / fn).write_bytes(src.read_bytes())


def s3_client():
    """创建 S3/R2 客户端"""
    _require_env("S3_ENDPOINT_URL", S3_ENDPOINT_URL)
    _require_env("S3_BUCKET", S3_BUCKET)
    _require_env("S3_ACCESS_KEY_ID", S3_ACCESS_KEY_ID)
    _require_env("S3_SECRET_ACCESS_KEY", S3_SECRET_ACCESS_KEY)
    
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        region_name=os.environ.get("S3_REGION", "auto"),
    )


def comfy_post_prompt(prompt: dict) -> str:
    """提交工作流到 ComfyUI"""
    r = requests.post(f"{COMFY_BASE}/prompt", json={"prompt": prompt}, timeout=60)
    r.raise_for_status()
    return r.json()["prompt_id"]


def comfy_get_history(prompt_id: str) -> dict:
    """获取 ComfyUI 历史记录"""
    r = requests.get(f"{COMFY_BASE}/history/{prompt_id}", timeout=60)
    r.raise_for_status()
    return r.json()


def wait_until_done(prompt_id: str, timeout_sec: int = 3600, poll: float = 1.0) -> dict:
    """等待 ComfyUI 任务完成"""
    t0 = time.time()
    while True:
        hist = comfy_get_history(prompt_id)
        if prompt_id in hist:
            status = hist[prompt_id].get("status", {})
            if status.get("completed", False) or status.get("status_str") == "success":
                return hist[prompt_id]
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI error: {json.dumps(status, ensure_ascii=False)}")
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"ComfyUI job timeout after {timeout_sec}s, prompt_id={prompt_id}")
        time.sleep(poll)


def find_latest_output(prefix: str) -> Path:
    """在 ComfyUI 输出目录找最新的 mp4 文件"""
    best = None
    best_mtime = -1
    if not COMFY_OUTPUT_DIR.is_dir():
        raise RuntimeError(f"Missing output dir: {COMFY_OUTPUT_DIR}")
    
    for fn in os.listdir(COMFY_OUTPUT_DIR):
        if not fn.startswith(prefix):
            continue
        if not fn.lower().endswith(".mp4"):
            continue
        p = COMFY_OUTPUT_DIR / fn
        try:
            mt = p.stat().st_mtime
            if mt > best_mtime:
                best_mtime = mt
                best = p
        except Exception:
            continue
    
    if not best:
        raise RuntimeError(f"No output mp4 found for prefix={prefix} in {COMFY_OUTPUT_DIR}")
    return best


def deep_replace(obj: Any, mapping: Dict[str, str]) -> Any:
    """递归替换 JSON 结构中的占位符字符串"""
    if isinstance(obj, dict):
        return {k: deep_replace(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_replace(v, mapping) for v in obj]
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace(k, v)
        return obj
    return obj


# ========================================
# 主处理函数
# ========================================
def handler(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    RunPod Serverless Handler
    
    入参：
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
    """
    inp = (event or {}).get("input") or {}
    
    # 解析参数
    job_id = inp.get("job_id") or uuid.uuid4().hex[:12]
    input_key = inp.get("input_key")
    output_key = inp.get("output_key") or f"outputs/video-object-removal/{job_id}.mp4"
    
    params = inp.get("params") or {}
    remove_text = (params.get("remove_text") or inp.get("remove_text") or "").strip() or "object"
    timeout_sec = int(params.get("timeout_sec", 3600))
    
    # 验证必要参数
    if not input_key:
        return {"error": "missing input_key"}
    
    _require_env("S3_BUCKET", S3_BUCKET)
    
    # 应用 Florence2 patch
    apply_florence2_patch()
    
    # 创建工作目录
    workdir = Path(f"/tmp/jobs/{job_id}")
    workdir.mkdir(parents=True, exist_ok=True)
    
    local_in = workdir / "in.mp4"
    local_pre = workdir / "in_pre.mp4"
    
    s3 = s3_client()
    t0 = time.time()
    
    try:
        # ========================================
        # 1) 从 R2 下载输入视频
        # ========================================
        print(f"[handler] Downloading: {input_key}", flush=True)
        s3.download_file(S3_BUCKET, input_key, str(local_in))
        t_dl = time.time()
        
        # ========================================
        # 2) 视频预处理
        # ========================================
        print(f"[handler] Preprocessing video...", flush=True)
        pre_info = preprocess_video(str(local_in), str(local_pre), fps_cap=FPS_CAP, max_seconds=MAX_SECONDS)
        input_path_for_comfy = pre_info["path"]
        t_pre = time.time()
        
        # 复制预处理后的视频到 ComfyUI input 目录
        COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        comfy_input_file = COMFY_INPUT_DIR / f"{job_id}.mp4"
        shutil.copy(input_path_for_comfy, comfy_input_file)
        
        # ========================================
        # 3) 加载并修改工作流
        # ========================================
        print(f"[handler] Loading workflow...", flush=True)
        if not WORKFLOW_PATH.exists():
            raise RuntimeError(f"Workflow not found: {WORKFLOW_PATH}")
        
        wf = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
        
        output_prefix = f"{job_id}_video-object-removal"
        
        # 修改工作流参数
        # 节点 25: VHS_LoadVideo -> video 文件名
        if "25" in wf and "inputs" in wf["25"]:
            wf["25"]["inputs"]["video"] = comfy_input_file.name
        
        # 节点 30: Primitive string multiline -> 要移除的对象文本
        if "30" in wf and "inputs" in wf["30"]:
            wf["30"]["inputs"]["string"] = remove_text
        
        # 节点 13: VHS_VideoCombine -> 输出文件名前缀
        if "13" in wf and "inputs" in wf["13"]:
            wf["13"]["inputs"]["filename_prefix"] = output_prefix
        
        t_wf = time.time()
        
        # ========================================
        # 4) 执行 ComfyUI 工作流
        # ========================================
        print(f"[handler] Submitting to ComfyUI...", flush=True)
        prompt_id = comfy_post_prompt(wf)
        print(f"[handler] prompt_id: {prompt_id}", flush=True)
        
        result = wait_until_done(prompt_id, timeout_sec=timeout_sec)
        t_comfy = time.time()
        
        # ========================================
        # 5) 上传输出到 R2
        # ========================================
        print(f"[handler] Finding output...", flush=True)
        out_local = find_latest_output(output_prefix)
        print(f"[handler] Output file: {out_local}", flush=True)
        
        print(f"[handler] Uploading to: {output_key}", flush=True)
        s3.upload_file(str(out_local), S3_BUCKET, output_key)
        t_up = time.time()
        
        # 清理输出文件（避免磁盘占满）
        try:
            out_local.unlink()
        except Exception:
            pass
        
        return {
            "job_id": job_id,
            "input_key": input_key,
            "output_key": output_key,
            "remove_text": remove_text,
            "prompt_id": prompt_id,
            "preprocess": pre_info,
            "timing_sec": {
                "download": round(t_dl - t0, 3),
                "preprocess": round(t_pre - t_dl, 3),
                "workflow_prepare": round(t_wf - t_pre, 3),
                "comfy_run": round(t_comfy - t_wf, 3),
                "upload": round(t_up - t_comfy, 3),
                "total": round(t_up - t0, 3),
            },
            "comfy_status": result.get("status", {}),
        }
    
    finally:
        # 清理工作目录
        shutil.rmtree(workdir, ignore_errors=True)


# ========================================
# 启动 RunPod Serverless
# ========================================
runpod.serverless.start({"handler": handler})
