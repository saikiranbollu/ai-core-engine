"""Lightweight secret wrapper to keep credentials off plaintext attrs (F-CF-X02).

``SecretStr`` holds a credential without exposing it through ``repr``/``str`` and
supports best-effort zeroing on close so connector tokens/passwords are not left
in plaintext on instance attributes or leaked into logs and memory dumps.

This is stdlib-only (no pydantic dependency) so it is safe to import everywhere
in the ingestion pipeline.
"""
from __future__ import annotations

from typing import Optional

__all__ = ["SecretStr"]


class SecretStr:
    """Opaque holder for a sensitive string value.

    The value is retrievable via :meth:`get` but never appears in ``repr`` or
    ``str``. :meth:`clear` drops the reference (best-effort zeroize) so callers
    can scrub credentials on close.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Optional[str]) -> None:
        self._value: Optional[str] = value

    def get(self) -> str:
        """Return the wrapped secret (empty string once cleared)."""
        return self._value or ""

    def clear(self) -> None:
        """Drop the secret reference. Subsequent :meth:`get` returns ``""``."""
        self._value = None

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "SecretStr('***')" if self._value else "SecretStr(None)"

    __str__ = __repr__
