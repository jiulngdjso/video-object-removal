#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Install ComfyUI custom nodes from a lock file (repo|commit) into a target folder.

Lock file format (one per line):
  https://github.com/xxx/yyy.git|<commit_sha>
Comments/blank lines allowed.

Example:
  python tools/install_custom_nodes.py locks/custom_nodes.lock.txt /comfyui/custom_nodes
"""

import os
import sys
import time
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple


def run(cmd: List[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    # Print command for visibility in docker build logs
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, text=True)


def run_ok(cmd: List[str], cwd: Path | None = None) -> bool:
    try:
        run(cmd, cwd=cwd, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def parse_lock(lock_path: Path) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for raw in lock_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # allow "repo|sha" or "repo sha"
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
    # https://github.com/a/b.git -> b
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def ensure_git_exists() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("git not found in PATH")


def safe_backup_non_git_dir(dst: Path) -> None:
    # If folder exists but not a git repo (common when Manager leaves residue), move it away.
    if dst.exists() and dst.is_dir() and not (dst / ".git").exists():
        bk = dst.with_name(dst.name + f".bak.{int(time.time())}")
        print(f"[WARN] {dst} exists but not a git repo. Move to {bk}", flush=True)
        dst.rename(bk)


def checkout_exact_commit(dst: Path, repo_url: str, sha: str, retries: int = 2) -> None:
    """
    Strategy:
      - if dir doesn't exist: clone (no checkout) then fetch commit and checkout
      - if exists: set remote, fetch commit, checkout
    Use shallow fetch by commit first; if fails, fallback to normal fetch.
    """
    # Make sure repo is initialized
    if not (dst / ".git").exists():
        # clone as bare minimal
        # --filter=blob:none reduces transfer size on GitHub
        ok = False
        for i in range(retries + 1):
            try:
                run(["git", "clone", "--no-checkout", "--filter=blob:none", repo_url, str(dst)])
                ok = True
                break
            except subprocess.CalledProcessError:
                if i >= retries:
                    raise
                print(f"[WARN] clone failed, retry {i+1}/{retries} ...", flush=True)
                time.sleep(2)
        if not ok:
            raise RuntimeError(f"Failed to clone {repo_url}")

    # Ensure remote origin correct
    run_ok(["git", "remote", "remove", "origin"], cwd=dst)
    run(["git", "remote", "add", "origin", repo_url], cwd=dst)

    # Try shallow fetch exact commit
    fetched = False
    for i in range(retries + 1):
        if run_ok(["git", "fetch", "--depth", "1", "origin", sha], cwd=dst):
            fetched = True
            break
        if i < retries:
            print(f"[WARN] shallow fetch commit failed, retry {i+1}/{retries} ...", flush=True)
            time.sleep(2)

    # Fallback: fetch default refs if commit fetch didn't work
    if not fetched:
        print("[WARN] fallback to full fetch (may be slower)...", flush=True)
        # prune old refs
        run_ok(["git", "fetch", "--all", "--prune"], cwd=dst)
        fetched = True

    # Checkout commit (detached HEAD)
    # If the repo uses submodules, you'd add submodule update here, but most ComfyUI nodes don't.
    run(["git", "checkout", "--force", sha], cwd=dst)

    # Optional: show status
    run_ok(["git", "rev-parse", "HEAD"], cwd=dst)


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: install_custom_nodes.py <lock_file> <custom_nodes_dir>", file=sys.stderr)
        return 2

    lock_file = Path(sys.argv[1]).resolve()
    target_dir = Path(sys.argv[2]).resolve()

    if not lock_file.exists():
        print(f"[ERROR] lock file not found: {lock_file}", file=sys.stderr)
        return 2

    ensure_git_exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    items = parse_lock(lock_file)
    print(f"[INFO] lock items: {len(items)}", flush=True)
    if not items:
        print("[WARN] lock file empty, nothing to do.", flush=True)
        return 0

    # Controls
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
            # In serverless image build, failing early is usually better:
            return 1

    # export a quick summary (optional)
    print("\n[INFO] Installed nodes:", flush=True)
    for repo_url, sha in items:
        name = repo_dir_name(repo_url)
        dst = target_dir / name
        if (dst / ".git").exists():
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(dst), text=True).strip()
            print(f" - {name}: {head}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
