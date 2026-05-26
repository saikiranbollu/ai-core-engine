"""Shared Cypher safety utilities for Knowledge Graph operations.

MEG_SW-361, MEG_SW-367, MEG_SW-379, MEG_SW-386:
Defense-in-depth sanitization for Neo4j label and relationship-type names
interpolated into Cypher queries. Prevents injection if label sources ever
become user-controlled.
"""
from __future__ import annotations

import re

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def sanitize_label(name: str) -> str:
    """Validate and return a Neo4j label/relationship-type name.

    Raises ValueError if the name contains characters outside
    [A-Za-z0-9_] or does not start with a letter.
    """
    if not name or not _SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"Invalid Neo4j identifier: {name!r}. "
            "Must match ^[A-Za-z][A-Za-z0-9_]*$"
        )
    return name


def sanitize_property(name: str) -> str:
    """Validate a Neo4j property name used in Cypher interpolation."""
    if not name or not _SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"Invalid Neo4j property name: {name!r}. "
            "Must match ^[A-Za-z][A-Za-z0-9_]*$"
        )
    return name
