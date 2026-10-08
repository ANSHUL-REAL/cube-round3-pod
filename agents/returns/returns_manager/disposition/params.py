"""Disposition parameters (§8.6) and the rules version (§8.6):
`rules_version = "disposition-" + first 12 hex of
SHA-256(JCS({engine_source_sha256, params_content_sha256}))`.
Changing the engine's code or the parameters therefore always changes the version written into decisions.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from returns_manager.canonical.hashing import sha256_jcs, sha256_text
from returns_manager.config import REPO_ROOT
from returns_manager.reference.models import DispositionParamsV1

PARAMS_FILE = REPO_ROOT / "reference" / "rules" / "disposition-params.yaml"
ENGINE_FILE = Path(__file__).with_name("engine.py")


@lru_cache(maxsize=2)
def load_params(path: Path = PARAMS_FILE) -> DispositionParamsV1:
    return DispositionParamsV1.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def rules_version(params: DispositionParamsV1 | None = None) -> str:
    p = params or load_params()
    engine_sha = sha256_text(ENGINE_FILE.read_bytes())
    digest = sha256_jcs({"engine_source_sha256": engine_sha, "params_content_sha256": p.content_sha256 or ""})
    return "disposition-" + digest[:12]
