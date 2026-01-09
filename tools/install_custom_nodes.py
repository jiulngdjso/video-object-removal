#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Install ComfyUI custom nodes from a lock file (repo|commit) into a target folder.

Lock file format (one per line):
  https://github.com/xxx/yyy.git|<commit_sha>
Comments/blank lines allowed.

Usage:
  python tools/install_custom_nodes.py --lock locks/custom_nodes.lock.txt --dst /comfyui/custom_nodes
or:
  python tools/install_custom_nodes.py locks/custom_nodes.lock.txt /comfyui/custom_nodes
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional


def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True)


def run_ok(cmd: List[str], cwd: Optional[Path] = None) -> bool:
    try:
        run(cmd, cwd=cwd, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def ensure_git_exists() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("git not found in PATH")


def parse_lock(lock_path: Path) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for raw in lock_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            repo, sha = line.split("|", 1)
        else:
            parts = line.split()
            if len(parts) < 2:
                raise ValueError(f"Bad lock line: {raw!r}")
            repo, sha = parts[0], parts[1]
        repo, sha = repo.strip(), sha.strip()
        if not repo or not sha:
            raise ValueError(f"Bad lock line: {raw!r}")
        items.append((repo, sha))
    return items


def repo_dir_name(repo_url: str) -> str:
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def safe_backup_non_git_dir(dst: Path) -> None:
    if dst.exists() and dst.is_dir() and not (dst / ".git").exists():
        bk = dst.with_name(dst.name + f".bak.{int(time.time())}")
        print(f"[WARN] {dst} exists but not a git repo. Move to {bk}", flush=True)
        dst.rename(bk)


def checkout_exact_commit(dst: Path, repo_url: str, sha: str, retries: int = 2) -> None:
    """
    Robust strategy for RunPod build:
      - clone without partial-filter (avoid missing-blob issues)
      - try fetch exact commit with depth=1
      - if fails, unshallow / full fetch, then checkout
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    # clone if not exists
    if not (dst / ".git").exists():
        ok = False
        for i in range(retries + 1):
            try:
                # no --filter=blob:none (this is the common cause of missing blob in some build envs)
                run(["git", "clone", "--no-checkout", repo_url, str(dst)], check=True)
                ok = True
                break
            except subprocess.CalledProcessError:
                if i >= retries:
                    raise
                print(f"[WARN] clone failed, retry {i+1}/{retries} ...", flush=True)
                time.sleep(2)
        if not ok:
            raise RuntimeError(f"Failed to clone {repo_url}")

    # ensure origin
    run_ok(["git", "remote", "remove", "origin"], cwd=dst)
    run(["git", "remote", "add", "origin", repo_url], cwd=dst)

    # 1) try shallow fetch the commit
    fetched = False
    for i in range(retries + 1):
        if run_ok(["git", "fetch", "--depth", "1", "origin", sha], cwd=dst):
            fetched = True
            break
        if i < retries:
            print(f"[WARN] fetch commit failed, retry {i+1}/{retries} ...", flush=True)
            time.sleep(2)

    # 2) fallback: unshallow or full fetch
    if not fetched:
        print("[WARN] fallback to full fetch ...", flush=True)
        # Try unshallow first
        if not run_ok(["git", "fetch", "--unshallow", "origin"], cwd=dst):
            run_ok(["git", "fetch", "--all", "--tags", "--prune"], cwd=dst)

    # checkout detached
    run(["git", "checkout", "--force", sha], cwd=dst)

    # sanity
    run_ok(["git", "rev-parse", "HEAD"], cwd=dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", dest="lock_file", default=None, help="lock file path (repo|commit)")
    ap.add_argument("--dst", dest="dst_dir", default=None, help="custom_nodes dir")
    ap.add_argument("positional", nargs="*", help="optional: <lock_file> <dst_dir>")
    args = ap.parse_args()

    lock_file = args.lock_file
    dst_dir = args.dst_dir

    if (not lock_file or not dst_dir) and len(args.positional) >= 2:
        lock_file = lock_file or args.positional[0]
        dst_dir = dst_dir or args.positional[1]

    if not lock_file or not dst_dir:
        print("Usage: install_custom_nodes.py --lock <lock_file> --dst <custom_nodes_dir>", file=sys.stderr)
        return 2

    lock_path = Path(lock_file).resolve()
    target_dir = Path(dst_dir).resolve()

    if not lock_path.exists():
        print(f"[ERROR] lock file not found: {lock_path}", file=sys.stderr)
        return 2

    ensure_git_exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    items = parse_lock(lock_path)
    print(f"[INFO] lock items: {len(items)}", flush=True)
    if not items:
        print("[WARN] lock file empty, nothing to do.", flush=True)
        return 0

    retries = int(os.environ.get("GIT_RETRIES", "2"))

    for repo_url, sha in items:
        name = repo_dir_name(repo_url)
        dst = target_dir / name
        print(f"\n==== {name} ====", flush=True)
        print(f"[repo] {repo_url}", flush=True)
        print(f"[sha ] {sha}", flush=True)

        safe_backup_non_git_dir(dst)

        try:
            checkout_exact_commit(dst, repo_url, sha, retries=retries)
            print(f"[OK] {name} pinned at {sha}", flush=True)
        except Exception as e:
            print(f"[FAIL] {name}: {e}", file=sys.stderr, flush=True)
            return 1

    print("\n[INFO] Installed nodes:", flush=True)
    for repo_url, _sha in items:
        name = repo_dir_name(repo_url)
        dst = target_dir / name
        if (dst / ".git").exists():
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(dst), text=True).strip()
            print(f" - {name}: {head}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
