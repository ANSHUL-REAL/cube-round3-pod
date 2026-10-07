"""Disposition parameters and the rules version (Round 2 `disposition/params.py`).

`rules_version = "disposition-" + first 12 hex of SHA-256(JCS({engine_source_sha256, params_content_sha256}))`, so
changing the engine's code or the parameters always changes the version written into decisions.

Round 3 changes: paths point at agents/returns; the parameter file's own content hash is verified on load.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .canonical import sha256_jcs, sha256_text
from .errors import VerificationFailed
from .models import DispositionParamsV1
from .paths import REFERENCE_DIR
from .refhash import verify_reference_content_sha256

PARAMS_FILE = REFERENCE_DIR / "rules" / "disposition-params.yaml"
ENGINE_FILE = Path(__file__).with_name("engine.py")


@lru_cache(maxsize=2)
def load_params(path: Path = PARAMS_FILE) -> DispositionParamsV1:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not verify_reference_content_sha256(doc):
        raise VerificationFailed(f"{path.name}: content_sha256 does not match the file")
    return DispositionParamsV1.model_validate(doc)


def rules_version(params: DispositionParamsV1 | None = None) -> str:
    p = params or load_params()
    engine_sha = sha256_text(ENGINE_FILE.read_bytes())
    digest = sha256_jcs({"engine_source_sha256": engine_sha, "params_content_sha256": p.content_sha256 or ""})
    return "disposition-" + digest[:12]
