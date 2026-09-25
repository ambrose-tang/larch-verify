"""Persistence of human-approved specifications.

Approved specs are the durable asset: once a developer has signed off on "what this
function must do", later runs (and CI) reuse them instead of re-generating. They are
stored in `<project>/.larch/specs/` when the project has opted in with `larch init`,
otherwise in the user cache.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..py.extract import FunctionInfo
from ..spec import FormalSpec
from ..util import cache_root, sha256, slug


def project_root(path: Path) -> Path:
    d = path.resolve().parent
    for cand in [d, *d.parents]:
        if (cand / ".larch").is_dir() or (cand / ".git").exists() or (cand / "pyproject.toml").exists():
            return cand
    return d


def _spec_path(info: FunctionInfo) -> Path:
    root = project_root(info.path)
    rel = info.path.resolve().relative_to(root) if info.path.resolve().is_relative_to(root) else Path(info.path.name)
    if (root / ".larch").is_dir():
        return root / ".larch" / "specs" / rel.with_suffix("") / f"{info.name}.json"
    return cache_root() / "approved" / sha256(str(root))[:12] / rel.with_suffix("") / f"{slug(info.name)}.json"


def save_approved(info: FunctionInfo, spec: FormalSpec, *, auto: bool) -> Path | None:
    if auto:
        return None  # only human-reviewed specs are reused
    p = _spec_path(info)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "larch_spec_version": 1,
        "function": info.name,
        "file": info.path.name,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "source_sha256": sha256(info.source),
        "fingerprint": spec.fingerprint(),
        "spec": spec.to_json(),
    }
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return p


def load_approved(info: FunctionInfo) -> tuple[FormalSpec, str] | None:
    p = _spec_path(info)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text())
        spec = FormalSpec.from_json(doc["spec"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    # Parameters must still line up with the current signature.
    if [x.name for x in spec.params] != [x.lean_name for x in info.params]:
        return None
    return spec, doc.get("approved_at", "an earlier run")
