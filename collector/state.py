#!/usr/bin/env python3
"""Keeps the Robinhood OAuth tokens between collector jobs.

Robinhood hands out a NEW refresh token on every refresh, so each job saves the new one for the next. It is stored ENCRYPTED
(Fernet: AES-128-CBC + HMAC) as `rh_token.enc` on the orphan branch `runner-state` of a separate PRIVATE repo ($STATE_REPO,
reached with the write deploy key in the secret STATE_DEPLOY_KEY), force-pushed as a single commit (no history of old tokens).
The encryption key is the secret RH_TOKEN_KEY. Nothing about the login is ever stored in this public repo.

  python collector/state.py pull   # decrypt runner-state:rh_token.enc -> $RH_TOKEN_FILE
  python collector/state.py push   # encrypt $RH_TOKEN_FILE -> runner-state:rh_token.enc (rh.py does this after each refresh)
"""
import os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANCH = "runner-state"
BLOB = "rh_token.enc"


def _env():
    """git over SSH with the deploy key from $STATE_DEPLOY_KEY (written to a 0600 temp file once per process)."""
    env = dict(os.environ)
    key = os.environ.get("STATE_DEPLOY_KEY", "").replace("\r", "").strip()
    if key:
        kf = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "state_deploy_key"
        if not kf.exists():
            kf.write_text(key + "\n"); os.chmod(kf, 0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {kf} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return env


def remote():
    return os.environ.get("STATE_REMOTE") or f"git@github.com:{os.environ.get('STATE_REPO', 'pwoo32/alpha-desk-state')}.git"


def _git(*a, inp=None):
    r = subprocess.run(["git", *a], cwd=ROOT, input=inp, capture_output=True, env=_env())
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


def push_blob(data: bytes, msg="runner state"):
    """Write `data` as rh_token.enc in a parentless commit and force-push it to origin/runner-state. Leaves the work tree alone."""
    blob = _git("hash-object", "-w", "--stdin", inp=data).decode().strip()
    tree = _git("mktree", inp=f"100644 blob {blob}\t{BLOB}\n".encode()).decode().strip()
    env_ident = ["-c", "user.name=collector", "-c", "user.email=collector@users.noreply.github.com"]
    commit = _git(*env_ident, "commit-tree", tree, "-m", msg).decode().strip()
    last = None
    for _ in range(4):
        try:
            _git("push", "--force", remote(), f"{commit}:refs/heads/{BRANCH}"); return commit
        except RuntimeError as e:
            last = e; time.sleep(5)
    raise last


def pull(dest=None):
    dest = Path(dest or token_file())
    _git("fetch", "-q", remote(), f"+refs/heads/{BRANCH}:refs/state/{BRANCH}")
    enc = _git("show", f"refs/state/{BRANCH}:{BLOB}")
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
