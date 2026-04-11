# AICE Ingestion Pipeline - Incremental Ingestion Module
"""Incremental ingestion with git tracking and file-change detection."""

from .incremental_ingestion import (
    IncrementalIngestionManager,
    IncrementalState,
    FileChangeRecord,
    DeltaReport,
    ChangeType,
)

__all__ = [
    "IncrementalIngestionManager",
    "IncrementalState",
    "FileChangeRecord",
    "DeltaReport",
    "ChangeType",
]
