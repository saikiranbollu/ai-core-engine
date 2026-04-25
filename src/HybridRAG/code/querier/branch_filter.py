"""
Branch Tag Filter Utilities
============================
Generates Neo4j WHERE clauses and Qdrant FieldConditions for
multi-branch RAG isolation.

Supports:
  - Single branch tag:  ``branch_tag="release/2.0"``
  - Multiple tags:      ``branch_tag=["release/2.0", "common"]``
  - No filter (None):   backward-compatible, matches all branches

Usage:
    from querier.branch_filter import neo4j_branch_clause, qdrant_branch_condition

    # Neo4j
    clause, params = neo4j_branch_clause("release/2.0", node_alias="n")
    cypher = f"MATCH (n:APIFunction) WHERE {clause} RETURN n"

    # Qdrant
    condition = qdrant_branch_condition("release/2.0")
    # add to qdrant Filter(must=[condition, ...])
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from qdrant_client.models import FieldCondition, MatchAny, MatchValue


def neo4j_branch_clause(
    branch_tag: Optional[Union[str, List[str]]] = None,
    node_alias: str = "n",
    param_name: str = "branch_tag",
) -> Tuple[str, Dict[str, Any]]:
    """Return a Cypher WHERE fragment and parameters for branch filtering.

    Parameters
    ----------
    branch_tag : str, list[str], or None
        Branch tag(s) to filter on.  None disables filtering.
    node_alias : str
        Node alias used in the MATCH clause (default ``"n"``).
    param_name : str
        Cypher parameter name (default ``"branch_tag"``).

    Returns
    -------
    (clause, params)
        *clause* is a string like ``"n.branch_tag = $branch_tag"`` or
        ``"n.branch_tag IN $branch_tag"`` or ``"1=1"`` (no filter).
        *params* is the dict to merge into ``tx.run()`` kwargs.
    """
    if branch_tag is None:
        return "1=1", {}

    if isinstance(branch_tag, str):
        return (
            f"{node_alias}.branch_tag = ${param_name}",
            {param_name: branch_tag},
        )

    # List of tags
    tags = list(branch_tag)
    if len(tags) == 1:
        return (
            f"{node_alias}.branch_tag = ${param_name}",
            {param_name: tags[0]},
        )
    return (
        f"{node_alias}.branch_tag IN ${param_name}",
        {param_name: tags},
    )


def qdrant_branch_condition(
    branch_tag: Optional[Union[str, List[str]]] = None,
    field_name: str = "branch_tag",
) -> Optional[FieldCondition]:
    """Return a Qdrant FieldCondition for branch filtering, or None.

    Parameters
    ----------
    branch_tag : str, list[str], or None
        Branch tag(s) to match.  None returns None (no filter).
    field_name : str
        Payload field name in Qdrant (default ``"branch_tag"``).

    Returns
    -------
    FieldCondition or None
    """
    if branch_tag is None:
        return None

    if isinstance(branch_tag, str):
        return FieldCondition(
            key=field_name,
            match=MatchValue(value=branch_tag),
        )

    tags = list(branch_tag)
    if len(tags) == 1:
        return FieldCondition(
            key=field_name,
            match=MatchValue(value=tags[0]),
        )

    return FieldCondition(
        key=field_name,
        match=MatchAny(any=tags),
    )
