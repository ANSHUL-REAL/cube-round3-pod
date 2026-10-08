"""Versioned, hash-locked prompt files (§8.11).

`agent/prompts/<agent>/<name>.md` start with YAML front matter (`prompt_id`, `version`, `summary`).
`agent/prompts/prompts.lock.json` maps `prompt_id -> {version, sha256}` where the hash is over the
LF-normalised UTF-8 file. `verify_lock()` fails when a file changed without a version bump and a lock
update, so a prompt can never change silently. Refresh the lock deliberately with
`python -m returns_manager.llm.prompts --lock`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from returns_manager.canonical.hashing import normalize_text, sha256_hex
from returns_manager.config import AGENT_ROOT
from returns_manager.errors import VerificationFailed

PROMPTS_DIR = AGENT_ROOT / "prompts"
LOCK_FILE = PROMPTS_DIR / "prompts.lock.json"


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    version: str
    summary: str
    body: str
    sha256: str
    path: Path

    @property
    def ref(self) -> str:
        """`<prompt_id>-<version>`, as written into check `model_version` strings."""
        return f"{self.prompt_id}-{self.version}"


def parse_prompt(path: Path) -> Prompt:
    raw = normalize_text(path.read_bytes())
    text = raw.decode("utf-8")
    if not text.startswith("---\n"):
        raise VerificationFailed(f"{path.name}: missing YAML front matter")
    _, front, body = text.split("---\n", 2)
    meta = yaml.safe_load(front)
    for key in ("prompt_id", "version", "summary"):
        if not meta.get(key):
            raise VerificationFailed(f"{path.name}: front matter lacks {key}")
    return Prompt(
        prompt_id=str(meta["prompt_id"]),
        version=str(meta["version"]),
        summary=str(meta["summary"]),
        body=body.strip("\n"),
        sha256=sha256_hex(raw),
        path=path,
    )


def all_prompts(directory: Path = PROMPTS_DIR) -> dict[str, Prompt]:
    prompts: dict[str, Prompt] = {}
    for path in sorted(directory.rglob("*.md")):
        p = parse_prompt(path)
        if p.prompt_id in prompts:
            raise VerificationFailed(f"duplicate prompt_id {p.prompt_id!r}")
        prompts[p.prompt_id] = p
    return prompts


def verify_lock(directory: Path = PROMPTS_DIR, lock_file: Path = LOCK_FILE) -> dict[str, Prompt]:
    prompts = all_prompts(directory)
    lock = json.loads(lock_file.read_text(encoding="utf-8")) if lock_file.exists() else {}
    problems: list[str] = []
    for pid, p in prompts.items():
        entry = lock.get(pid)
        if entry is None:
            problems.append(f"{pid}: not in prompts.lock.json")
        elif entry["sha256"] != p.sha256:
            if entry["version"] == p.version:
                problems.append(f"{pid}: content changed without a version bump (still {p.version})")
            else:
                problems.append(f"{pid}: version bumped to {p.version} but the lock was not updated")
        elif entry["version"] != p.version:
            problems.append(f"{pid}: lock says version {entry['version']}, file says {p.version}")
    for pid in set(lock) - set(prompts):
        problems.append(f"{pid}: in prompts.lock.json but no prompt file")
    if problems:
        raise VerificationFailed("prompt lock check failed: " + "; ".join(problems))
    return prompts


def write_lock(directory: Path = PROMPTS_DIR, lock_file: Path = LOCK_FILE) -> None:
    prompts = all_prompts(directory)
    lock = {pid: {"version": p.version, "sha256": p.sha256} for pid, p in sorted(prompts.items())}
    lock_file.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")


@lru_cache(maxsize=1)
def locked_prompts() -> dict[str, Prompt]:
    """The prompts the running system uses: only ever the locked versions."""
    return verify_lock()


def get_prompt(prompt_id: str) -> Prompt:
    return locked_prompts()[prompt_id]


if __name__ == "__main__":
    if "--lock" in sys.argv:
        write_lock()
        print(f"wrote {LOCK_FILE}")
    else:
        for pid, p in verify_lock().items():
            print(f"{pid} {p.version} {p.sha256[:12]} ok")
