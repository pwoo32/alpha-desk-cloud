#!/usr/bin/env python3
"""Keeps the Robinhood OAuth tokens between collector jobs.

Robinhood hands out a NEW refresh token on every refresh, so each job saves the new one for the next. It is stored ENCRYPTED
(Fernet: AES-128-CBC + HMAC) as `rh_token.enc` on the orphan branch `runner-state`, force-pushed as a single commit (no history
of old tokens). The key is the Actions secret RH_TOKEN_KEY; without it the file is useless.

  python collector/state.py pull   # decrypt runner-state:rh_token.enc -> $RH_TOKEN_FILE
  python collector/state.py push   # encrypt $RH_TOKEN_FILE -> runner-state:rh_token.enc (rh.py does this after each refresh)
"""
import os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANCH = "runner-state"
BLOB = "rh_token.enc"


def _git(*a, inp=None):
    r = subprocess.run(["git", *a], cwd=ROOT, input=inp, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _fernet(key=None):
    from cryptography.fernet import Fernet
    key = key or os.environ.get("RH_TOKEN_KEY", "").strip()
    if not key:
        raise RuntimeError("RH_TOKEN_KEY is not set (Settings > Secrets and variables > Actions)")
    return Fernet(key.encode())


def token_file():
    return Path(os.environ.get("RH_TOKEN_FILE", Path.home() / ".tokens" / "rh_collector.json"))


def push_blob(data: bytes, msg="runner state", remote="origin"):
    """Write `data` as rh_token.enc in a parentless commit and force-push it to origin/runner-state. Leaves the work tree alone."""
    blob = _git("hash-object", "-w", "--stdin", inp=data).decode().strip()
    tree = _git("mktree", inp=f"100644 blob {blob}\t{BLOB}\n".encode()).decode().strip()
    env_ident = ["-c", "user.name=collector", "-c", "user.email=collector@users.noreply.github.com"]
    commit = _git(*env_ident, "commit-tree", tree, "-m", msg).decode().strip()
    last = None
    for _ in range(4):
        try:
            _git("push", "--force", remote, f"{commit}:refs/heads/{BRANCH}"); return commit
        except RuntimeError as e:
            last = e; time.sleep(5)
    raise last


def pull(dest=None):
    dest = Path(dest or token_file())
    _git("fetch", "-q", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    enc = _git("show", f"origin/{BRANCH}:{BLOB}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_fernet().decrypt(enc))
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def push(src=None):
    src = Path(src or token_file())
    return push_blob(_fernet().encrypt(src.read_bytes()), "encrypted Robinhood token")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pull": print("token restored to", pull())
    elif cmd == "push": print("token saved to", BRANCH, push())
    else: print(__doc__)
