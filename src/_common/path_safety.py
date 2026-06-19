"""Path-traversal and identifier-safety helpers (W-05).

Stdlib-only utilities shared by ingestion, OCR, and sandbox code paths to
defend against path traversal, symlink escapes, and unsafe identifiers before
values reach the filesystem or subprocess arguments.

Findings addressed: F-CA-I01 (ingest_file traversal), F-CE-O01 (OCR path/lang),
F-CB-09 (sandbox session_id), F-CF-X04 (connector sync-state dirs). All four
filesystem entry points contain their inputs under allowed roots via
``safe_path_under`` / ``allowed_roots_from_env``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Set, Union

__all__ = [
    "validate_session_id",
    "safe_path_under",
    "reject_traversal",
    "validate_extension",
    "allowed_roots_from_env",
]

_PathLike = Union[str, "os.PathLike[str]"]

# Session ids are embedded directly into filesystem paths, so restrict them to
# a conservative character set with no separators or traversal sequences.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_session_id(session_id: str) -> str:
    """Return *session_id* if it is a safe path component, else raise ``ValueError``.

    Rejects traversal sequences, path separators, and any character outside
    ``[A-Za-z0-9_-]`` so the id can be embedded in a filesystem path safely.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return session_id


def _resolve_no_symlink(path: _PathLike) -> Path:
    """Resolve *path*, rejecting a final-component symlink before resolution."""
    p = Path(path)
    if p.is_symlink():
        raise ValueError(f"Path is a symlink: {path}")
    return p.resolve()


def reject_traversal(path: _PathLike) -> Path:
    """Resolve *path*, rejecting parent-directory (``..``) traversal and symlinks.

    Does NOT enforce an allowed root, but any ``..`` component is rejected so the
    path cannot climb out of its intended directory, and a final-component
    symlink is refused. For full containment to a known directory, use
    :func:`safe_path_under`. Returns the resolved ``Path``.
    """
    if ".." in Path(path).parts:
        raise ValueError(f"Path contains parent-directory traversal: {path}")
    return _resolve_no_symlink(path)


def safe_path_under(path: _PathLike, allowed_roots: Iterable[_PathLike]) -> Path:
    """Resolve *path* and ensure it lives under one of *allowed_roots*.

    Raises ``ValueError`` if the path is a symlink or escapes every allowed
    root. Returns the resolved ``Path``.
    """
    roots = [Path(r).resolve() for r in allowed_roots]
    if not roots:
        raise ValueError("safe_path_under requires at least one allowed root")
    resolved = _resolve_no_symlink(path)
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"Path {str(path)!r} is not under allowed roots: {[str(r) for r in roots]}"
    )


def validate_extension(path: _PathLike, allowed: Set[str]) -> Path:
    """Return *path* as a ``Path`` if its suffix is in *allowed*, else raise.

    Extension comparison is case-insensitive.
    """
    p = Path(path)
    if p.suffix.lower() not in {e.lower() for e in allowed}:
        raise ValueError(
            f"Disallowed file extension {p.suffix!r}; allowed: {sorted(allowed)}"
        )
    return p


def allowed_roots_from_env(env_var: str, defaults: Iterable[_PathLike]) -> List[Path]:
    """Return the containment roots for *env_var*, falling back to *defaults*.

    The env var, when set, is a comma-separated list of directories. This keeps
    containment **on by default** (using *defaults*) while letting operators and
    tests redefine the allowed roots without code changes. Returned paths are not
    resolved here; :func:`safe_path_under` resolves them at check time.
    """
    raw = os.environ.get(env_var, "").strip()
    if raw:
        return [Path(r.strip()) for r in raw.split(",") if r.strip()]
    return [Path(d) for d in defaults]
