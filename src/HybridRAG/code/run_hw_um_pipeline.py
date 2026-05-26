#!/usr/bin/env python3
"""
Hardware User Manual → Neo4j Pipeline
=======================================

Full pipeline for ingesting AURIX hardware user manual content from ReqIF
into the knowledge graph. Fetches source data from Bitbucket, auto-discovers
all device families and their latest ReqIF versions, extracts module chapters
with registers/bitfields, uses LLM vision to describe diagrams and formulas,
and ingests into Neo4j.

When --module GPT12 is specified, the pipeline extracts GPT12 from ALL
available device ReqIF files (TC44x, TC45x, TC46x, etc.) — not just one.
Each node stores device family and UM version for full traceability.

Steps:
  0. Refresh LLM token (for vision API)
  1. Fetch HW UM repo from Bitbucket (shallow clone)
  2. Parse ReqIF and extract module chapter (per device)
  3. Describe images using LLM vision (formulas + diagrams)
  4. Ingest into Neo4j (HW_Module, HW_Section, HW_Register, HW_BitField, HW_Image)
  5. Create cross-links to existing SFR nodes

Usage:
  python run_hw_um_pipeline.py --module GPT12 --profile local
  python run_hw_um_pipeline.py --module ADC --profile mcal --clear
  python run_hw_um_pipeline.py --module ALL --profile test
  python run_hw_um_pipeline.py --module GPT12 --device TC44x --profile local
  python run_hw_um_pipeline.py --module GPT12 --device ALL --profile local
  python run_hw_um_pipeline.py --module GPT12 --only 2,3,4 --profile local
  python run_hw_um_pipeline.py --list-modules
  python run_hw_um_pipeline.py --list-devices
  python run_hw_um_pipeline.py --module GPT12 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CODE_DIR = Path(__file__).resolve().parent
HYBRIDRAG_DIR = CODE_DIR.parent
REPO_ROOT = HYBRIDRAG_DIR.parents[1]                   # ai-core-engine/
CONFIG_DIR = HYBRIDRAG_DIR / "config"
TEMP_DIR = REPO_ROOT / "temp"                          # ai-core-engine/temp/

# HW UM Repo structure
HW_UM_REPO_DIR = TEMP_DIR / "hw_um_repo"
HW_UM_BASE_DIR = HW_UM_REPO_DIR / "01_Hardware" / "05_UM"
HW_UM_REPO_SLUG = "aurix3g_sw_mcal_tc4xx_references"
BITBUCKET_BASE_URL = "https://bitbucket.vih.infineon.com"
BITBUCKET_PROJECT = "ATVA3GMCAL"

# Image cache (shared across runs, persistent)
IMAGE_CACHE_DIR = TEMP_DIR / "reqif_image_cache"

VALID_PROFILES = {"mcal", "test", "illd", "local"}

# Reliable Python executable
_VENV_DIR = HYBRIDRAG_DIR.parents[1] / ".venv"
if sys.platform == "win32":
    _VENV_PYTHON = _VENV_DIR / "Scripts" / "python.exe"
else:
    _VENV_PYTHON = _VENV_DIR / "bin" / "python"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

# Add code dir to path for imports
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
# Add KG dir to path
kg_dir = CODE_DIR / "KG"
if str(kg_dir) not in sys.path:
    sys.path.insert(0, str(kg_dir))


# ---------------------------------------------------------------------------
# Device / ReqIF Auto-Discovery
# ---------------------------------------------------------------------------
# Pattern: Infineon-AURIX-{device}-UM-v{major}_{minor}[-{patch}]-EN[_US].reqifz
_REQIFZ_PATTERN = re.compile(
    r"Infineon-AURIX-(?P<device>TC\w+?)(?:-N)?-UM-v(?P<version>[\d_]+)-EN(?:_US)?\.reqifz$",
    re.IGNORECASE,
)


@dataclass
class DeviceReqIF:
    """A discovered ReqIF file for a specific device family."""
    device: str            # e.g. "TC44x"
    version: str           # e.g. "00_90"
    version_tuple: tuple   # e.g. (0, 90) for sorting
    path: Path             # absolute path to .reqifz file
    filename: str          # basename

    @property
    def version_display(self) -> str:
        """Human-readable version like 'v00.90'."""
        parts = self.version.replace("_", ".").split(".")
        return "v" + ".".join(parts)


def _parse_version_tuple(version_str: str) -> tuple:
    """Parse version string like '00_90' or '01_1_1' into sortable tuple."""
    parts = version_str.split("_")
    return tuple(int(p) for p in parts)


def discover_device_reqifz(base_dir: Path = HW_UM_BASE_DIR) -> dict[str, DeviceReqIF]:
    """Auto-discover all device families and select latest ReqIF version for each.

    Scans the 01_Hardware/05_UM/ directory tree for .reqifz files,
    groups them by device family, and returns the latest version for each.
    Files in 'Archive/' subdirectories are excluded.

    Returns:
        Dict mapping device family (e.g. "TC44x") → DeviceReqIF with latest version.
    """
    if not base_dir.exists():
        return {}

    all_found: dict[str, list[DeviceReqIF]] = {}

    # Walk all .reqifz files recursively
    for reqifz_path in base_dir.rglob("*.reqifz"):
        # Skip files in Archive directories
        if "archive" in str(reqifz_path).lower():
            continue

        match = _REQIFZ_PATTERN.match(reqifz_path.name)
        if not match:
            # Handle non-standard names (e.g. "AURIX_TC46x_user_manual.reqifz")
            continue

        device = match.group("device")
        version = match.group("version")
        try:
            version_tuple = _parse_version_tuple(version)
        except ValueError:
            continue

        entry = DeviceReqIF(
            device=device,
            version=version,
            version_tuple=version_tuple,
            path=reqifz_path,
            filename=reqifz_path.name,
        )

        all_found.setdefault(device, []).append(entry)

    # Select latest version per device family
    latest: dict[str, DeviceReqIF] = {}
    for device, entries in all_found.items():
        entries.sort(key=lambda e: e.version_tuple, reverse=True)
        latest[device] = entries[0]

    return latest


def discover_device_reqifz_from_git() -> dict[str, DeviceReqIF]:
    """Discover all available device ReqIF files from git tree (even if not checked out).

    Uses `git ls-tree` to enumerate all .reqifz files in the repo without
    requiring them to be downloaded. This allows --list-devices to show
    everything available regardless of sparse-checkout state.

    Returns:
        Dict mapping device family → DeviceReqIF (latest version).
        Path will be the expected local path (may not exist yet).
    """
    if not (HW_UM_REPO_DIR / ".git").is_dir():
        return {}

    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "01_Hardware/05_UM/"],
            cwd=str(HW_UM_REPO_DIR),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}

    all_found: dict[str, list[DeviceReqIF]] = {}

    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line.endswith(".reqifz"):
            continue
        # Skip Archive paths
        if "/archive/" in line.lower() or "/Archive/" in line:
            continue

        filename = Path(line).name
        match = _REQIFZ_PATTERN.match(filename)
        if not match:
            continue

        device = match.group("device")
        version = match.group("version")
        try:
            version_tuple = _parse_version_tuple(version)
        except ValueError:
            continue

        entry = DeviceReqIF(
            device=device,
            version=version,
            version_tuple=version_tuple,
            path=HW_UM_REPO_DIR / line,  # Expected local path
            filename=filename,
        )
        all_found.setdefault(device, []).append(entry)

    # Select latest version per device
    latest: dict[str, DeviceReqIF] = {}
    for device, entries in all_found.items():
        entries.sort(key=lambda e: e.version_tuple, reverse=True)
        latest[device] = entries[0]

    return latest

logger = logging.getLogger("hw_um_pipeline")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_storage_config() -> dict:
    """Load storage_config.yaml with environment variable resolution."""
    storage_path = CONFIG_DIR / "storage_config.yaml"
    from env_config import load_yaml_with_env
    return load_yaml_with_env(storage_path)


def _get_neo4j_cfg(profile: str, storage_cfg: dict) -> dict:
    """Get Neo4j connection settings for a profile."""
    neo4j_section = storage_cfg.get("neo4j", {})
    if profile not in neo4j_section:
        available = list(neo4j_section.keys())
        print(f"\n  ERROR: Profile '{profile}' not found. Available: {available}\n")
        sys.exit(1)
    return neo4j_section[profile]


def _ask_profile() -> str:
    """Interactively ask which Neo4j instance to target."""
    print("\n" + "=" * 64)
    print("  Which Neo4j instance should the pipeline write to?")
    print("  1. test   — Test instance")
    print("  2. mcal   — Production MCAL")
    print("  3. illd   — ILLD")
    print("  4. local  — Local Neo4j Desktop (127.0.0.1:7687)")
    print("=" * 64)
    while True:
        choice = input("  Enter choice [1/2/3/4] (default=1 → test): ").strip()
        if choice in ("", "1", "test"):
            return "test"
        if choice in ("2", "mcal"):
            return "mcal"
        if choice in ("3", "illd"):
            return "illd"
        if choice in ("4", "local"):
            return "local"
        print("  Invalid choice.")


def _resolve_devices(device_arg: str) -> list[DeviceReqIF]:
    """Resolve the --device argument into a list of DeviceReqIF entries.

    Uses git ls-tree to discover all available devices (even if not yet
    downloaded). Falls back to filesystem discovery if git unavailable.

    Args:
        device_arg: "ALL" for all devices, or comma-separated device names
                    (e.g. "TC44x" or "TC44x,TC46x")

    Returns:
        List of DeviceReqIF objects representing which devices to process.
    """
    # Try git-tree discovery first (sees all devices), fall back to filesystem
    available = discover_device_reqifz_from_git()
    if not available:
        available = discover_device_reqifz()
    if not available:
        print(f"\n  ERROR: No .reqifz files found. Run with --auto-fetch to clone the repo.\n")
        sys.exit(1)

    if device_arg.upper() == "ALL":
        return sorted(available.values(), key=lambda d: d.device)

    devices = []
    for name in device_arg.split(","):
        name = name.strip()
        if name not in available:
            print(f"\n  ERROR: Device '{name}' not found.")
            print(f"  Available: {', '.join(sorted(available.keys()))}\n")
            sys.exit(1)
        devices.append(available[name])
    return devices


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path, label: str, dry_run: bool = False) -> None:
    """Run a subprocess, stream output, and abort on failure."""
    cmd_str = " ".join(cmd)
    logger.info("┌─ %s", label)
    logger.info("│  cwd: %s", cwd)
    logger.info("│  cmd: %s", cmd_str)

    if dry_run:
        logger.info("│  [DRY-RUN] skipped")
        logger.info("└─ %s (dry-run)\n", label)
        return

    start = time.perf_counter()
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    elapsed = time.perf_counter() - start

    if result.returncode != 0:
        logger.error("└─ FAILED: %s (exit %d, %.1fs)\n", label, result.returncode, elapsed)
        sys.exit(result.returncode)

    logger.info("└─ %s  (%.1fs)\n", label, elapsed)


# ---------------------------------------------------------------------------
# Pipeline Steps
# ---------------------------------------------------------------------------

def step0_token(dry_run: bool = False, **_) -> None:
    """Step 0: Refresh LLM token for vision API calls."""
    _run(
        [PYTHON, "token_manager.py"],
        cwd=CODE_DIR,
        label="Step 0: Refresh LLM token",
        dry_run=dry_run,
    )


def step1_fetch_repo(dry_run: bool = False, ref: str = "master", devices: list = None, **_) -> None:
    """Step 1: Fetch/update the HW UM reference repo from Bitbucket.

    Uses blobless clone with sparse checkout. Only downloads device folders
    that are actually needed for the current run (each reqifz is ~70MB).
    """
    clone_url = (
        f"{BITBUCKET_BASE_URL}/scm/"
        f"{BITBUCKET_PROJECT}/{HW_UM_REPO_SLUG}.git"
    )

    # Determine which device folders to include in sparse checkout
    device_folders = set()
    if devices:
        for dev in devices:
            # Extract the top-level folder (e.g. TC44x from the path)
            # Path is like: hw_um_repo/01_Hardware/05_UM/TC44x/file.reqifz
            rel = dev.path.relative_to(HW_UM_REPO_DIR)
            # rel = 01_Hardware/05_UM/TC44x/... — get the device folder part
            parts = rel.parts
            if len(parts) >= 3:
                device_folders.add(f"01_Hardware/05_UM/{parts[2]}")
    if not device_folders:
        device_folders = {"01_Hardware/05_UM"}

    if HW_UM_REPO_DIR.exists() and (HW_UM_REPO_DIR / ".git").is_dir():
        # Already cloned — update sparse checkout to include needed devices, then pull
        logger.info("┌─ Step 1: Update HW UM repo (ref=%s)", ref)
        logger.info("│  Repo exists at: %s", HW_UM_REPO_DIR)
        logger.info("│  Device folders needed: %s", ", ".join(sorted(device_folders)))
        if dry_run:
            logger.info("│  [DRY-RUN] Would update sparse-checkout and pull latest")
            logger.info("└─ Step 1 (dry-run)\n")
            return

        start = time.perf_counter()
        try:
            # Get current sparse-checkout paths and add new ones
            current = subprocess.run(
                ["git", "sparse-checkout", "list"],
                cwd=str(HW_UM_REPO_DIR),
                capture_output=True, text=True, timeout=10,
            )
            current_paths = set(current.stdout.strip().splitlines()) if current.returncode == 0 else set()
            all_paths = sorted(current_paths | device_folders)

            # Set sparse-checkout (additive — keeps previously downloaded devices)
            subprocess.run(
                ["git", "sparse-checkout", "set"] + all_paths,
                cwd=str(HW_UM_REPO_DIR),
                capture_output=True, text=True, timeout=600, check=True,
            )
            logger.info("│  Sparse checkout: %s", ", ".join(all_paths))

            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", ref],
                cwd=str(HW_UM_REPO_DIR),
                capture_output=True, text=True, timeout=300, check=True,
            )
            subprocess.run(
                ["git", "checkout", f"origin/{ref}"],
                cwd=str(HW_UM_REPO_DIR),
                capture_output=True, text=True, timeout=300, check=True,
            )
            elapsed = time.perf_counter() - start
            logger.info("│  Updated to latest %s", ref)
            logger.info("└─ Step 1  (%.1fs)\n", elapsed)
        except subprocess.CalledProcessError as exc:
            logger.warning("│  git update failed (using existing checkout): %s",
                         exc.stderr.strip() if exc.stderr else exc)
            logger.info("└─ Step 1  (using cached repo)\n")
    else:
        # Fresh clone with sparse-checkout for only needed device folders
        logger.info("┌─ Step 1: Clone HW UM repo from Bitbucket")
        logger.info("│  URL: %s", clone_url)
        logger.info("│  Target: %s", HW_UM_REPO_DIR)
        logger.info("│  Device folders: %s", ", ".join(sorted(device_folders)))

        if dry_run:
            logger.info("│  [DRY-RUN] Would clone repo")
            logger.info("└─ Step 1 (dry-run)\n")
            return

        HW_UM_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)

        start = time.perf_counter()
        # Clone with blobless filter and no checkout
        subprocess.run(
            [
                "git", "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--branch", ref,
                "--single-branch",
                clone_url,
                str(HW_UM_REPO_DIR),
            ],
            cwd=str(TEMP_DIR),
            capture_output=True, text=True, timeout=300, check=True,
        )
        # Set sparse-checkout to only needed device folders
        subprocess.run(
            ["git", "sparse-checkout", "init", "--cone"],
            cwd=str(HW_UM_REPO_DIR),
            capture_output=True, text=True, timeout=60, check=True,
        )
        subprocess.run(
            ["git", "sparse-checkout", "set"] + sorted(device_folders),
            cwd=str(HW_UM_REPO_DIR),
            capture_output=True, text=True, timeout=600, check=True,
        )
        subprocess.run(
            ["git", "checkout", ref],
            cwd=str(HW_UM_REPO_DIR),
            capture_output=True, text=True, timeout=600, check=True,
        )
        elapsed = time.perf_counter() - start
        logger.info("│  Cloned with sparse-checkout (%s)", ", ".join(sorted(device_folders)))
        logger.info("└─ Step 1  (%.1fs)\n", elapsed)


def step2_parse_reqif(module: str, devices: list, dry_run: bool = False, **_) -> None:
    """Step 2: Parse ReqIF and validate module chapter exists across all devices."""
    logger.info("┌─ Step 2: Validate ReqIF modules across %d device(s)", len(devices))

    if dry_run:
        for dev in devices:
            logger.info("│  [DRY-RUN] %s (%s) — %s", dev.device, dev.version_display, dev.filename)
        logger.info("└─ Step 2 (dry-run)\n")
        return

    start = time.perf_counter()
    from reqif_parser import ReqIFParser

    for dev in devices:
        if not dev.path.exists():
            print(f"\n  ERROR: ReqIF file not found: {dev.path}")
            print(f"  Run with --auto-fetch to clone the repo.\n")
            sys.exit(1)

        parser = ReqIFParser(dev.path)
        parser.load()
        available_modules = parser.get_module_names()

        if module.upper() == "ALL":
            logger.info("│  %s (%s): %d modules available",
                       dev.device, dev.version_display, len(available_modules))
        else:
            modules = [m.strip() for m in module.split(",")]
            missing = [m for m in modules if m not in available_modules]
            if missing:
                logger.warning("│  %s (%s): modules NOT found: %s",
                             dev.device, dev.version_display, ", ".join(missing))
                logger.info("│    Available: %s", ", ".join(sorted(available_modules)[:20]))
            else:
                logger.info("│  %s (%s): all modules validated ✓",
                           dev.device, dev.version_display)

    elapsed = time.perf_counter() - start
    logger.info("└─ Step 2  (%.1fs)\n", elapsed)


def step3_describe_images(
    module: str, devices: list, no_images: bool = False,
    dry_run: bool = False, **_
) -> None:
    """Step 3: LLM vision image description (handled inline by Step 4)."""
    if no_images:
        logger.info("┌─ Step 3: Image description (SKIPPED — --no-images)")
        logger.info("└─ Step 3 skipped\n")
        return

    logger.info("┌─ Step 3: LLM vision image description")
    logger.info("│  Cache dir: %s", IMAGE_CACHE_DIR)
    logger.info("│  Images will be described during ingestion (Step 4)")
    logger.info("│  Cached descriptions are reused across runs")
    logger.info("│  Devices: %s", ", ".join(d.device for d in devices))
    logger.info("└─ Step 3 (deferred to ingestion)\n")


def step4_ingest(
    module: str, devices: list, profile: str, no_images: bool = False,
    clear: bool = False, dry_run: bool = False, project: str = "A3G", **_
) -> None:
    """Step 4: Ingest parsed data into Neo4j.

    Creates HW_Module, HW_Section, HW_Register, HW_BitField, HW_Image nodes.
    Iterates over all specified device families, ingesting from each ReqIF.
    """
    storage_cfg = _load_storage_config()
    neo4j_cfg = _get_neo4j_cfg(profile, storage_cfg)

    from reqif_parser import ReqIFParser
    from reqif_kg_builder import ReqIFKnowledgeGraphBuilder

    total_devices = len(devices)
    for dev_idx, dev in enumerate(devices, 1):
        if not dev.path.exists():
            logger.warning("Skipping %s — file not found: %s", dev.device, dev.path)
            continue

        # Resolve module list for this device
        parser = ReqIFParser(dev.path)
        parser.load()
        available_modules = parser.get_module_names()

        if module.upper() == "ALL":
            modules = available_modules
        else:
            modules = [m.strip() for m in module.split(",")]
            # Only process modules that exist in this device's ReqIF
            present = [m for m in modules if m in available_modules]
            missing = [m for m in modules if m not in available_modules]
            if missing:
                logger.info("  %s: skipping modules not present: %s",
                           dev.device, ", ".join(missing))
            modules = present
            if not modules:
                logger.info("  %s: no requested modules found, skipping device", dev.device)
                continue

        total = len(modules)
        for idx, mod in enumerate(modules, 1):
            print(f"\n{'=' * 60}")
            print(f"  [{dev_idx}/{total_devices}] Device: {dev.device} ({dev.version_display})")
            print(f"  [{idx}/{total}] Ingesting module: {mod}")
            print(f"{'=' * 60}")

            builder = ReqIFKnowledgeGraphBuilder(
                neo4j_cfg=neo4j_cfg,
                reqifz_path=dev.path,
                module=mod,
                describe_images=not no_images,
                image_cache_dir=IMAGE_CACHE_DIR,
                dry_run=dry_run,
                clear_module=clear,
                device_variant=dev.device,
                um_version=dev.version_display,
                project=project,
            )
            builder.build()


# ---------------------------------------------------------------------------
# Val Repo Auto-Fetch (BVEC / TD / ConfigMap artefacts)
# ---------------------------------------------------------------------------

# Module name → val repo slug mapping
# Standard: aurix3g_sw_mcal_tc4xx_val_<module_lower>
# Multi-repo modules (ETH → leth, geth) are handled via MODULE_SUB_MODULES

# Directory suffixes and the sparse-checkout paths they need
_VAL_ARTEFACT_DIRS = {
    "specs": "00_Specs",     # BVEC
    "impl": "01_Implementation",  # TD
    "cfg": "02_Cfg",         # ConfigMap
}

# Cache of repo slugs that failed (repo not found) — avoids retrying across steps
_FAILED_REPO_SLUGS: set = set()


def _val_repo_slug(mcal_module: str) -> str:
    """Derive the Bitbucket val repo slug from a MCAL module name."""
    return f"aurix3g_sw_mcal_tc4xx_val_{mcal_module.lower()}"


def _ensure_val_repo(module: str, dry_run: bool = False) -> None:
    """Auto-fetch val repo for a module if not already present.

    Sparse-clones the val repo into temp/val_<module>_<suffix>/ directories
    for each artefact type (specs, impl, cfg).

    Skips silently if all directories already exist.
    """
    mcal_candidates = _expand_to_mcal_modules(module)

    for mcal_mod in mcal_candidates:
        mod_lower = mcal_mod.lower()

        # Check if any artefact directories are missing
        missing_dirs: list[tuple[str, str]] = []  # (suffix, sparse_path)
        for suffix, sparse_path in _VAL_ARTEFACT_DIRS.items():
            local_dir = TEMP_DIR / f"val_{mod_lower}_{suffix}"
            if not local_dir.is_dir() or not any(local_dir.iterdir()):
                missing_dirs.append((suffix, sparse_path))

        if not missing_dirs:
            continue  # All artefact dirs exist

        repo_slug = _val_repo_slug(mcal_mod)

        # Skip repos that already failed (not found) in this run
        if repo_slug in _FAILED_REPO_SLUGS:
            continue

        clone_url = f"{BITBUCKET_BASE_URL}/scm/{BITBUCKET_PROJECT}/{repo_slug}.git"

        logger.info("│  Auto-fetching val repo: %s", repo_slug)
        if dry_run:
            logger.info("│  [DRY-RUN] Would clone %s for: %s",
                        repo_slug, ", ".join(s for s, _ in missing_dirs))
            continue

        for suffix, sparse_path in missing_dirs:
            local_dir = TEMP_DIR / f"val_{mod_lower}_{suffix}"
            if local_dir.is_dir() and any(local_dir.iterdir()):
                continue

            logger.info("│  Cloning %s → val_%s_%s/ (sparse: %s)",
                        repo_slug, mod_lower, suffix, sparse_path)
            try:
                local_dir.mkdir(parents=True, exist_ok=True)

                # Blobless clone + sparse checkout
                subprocess.run(
                    ["git", "clone", "--filter=blob:none", "--no-checkout",
                     "--depth", "1", clone_url, str(local_dir)],
                    capture_output=True, text=True, timeout=300, check=True,
                )

                if suffix == "cfg":
                    # For cfg, use non-cone mode to fetch ONLY .xlsx files
                    # (avoids downloading hundreds of arxml config files)
                    subprocess.run(
                        ["git", "sparse-checkout", "init", "--no-cone"],
                        cwd=str(local_dir),
                        capture_output=True, text=True, timeout=10, check=True,
                    )
                    subprocess.run(
                        ["git", "sparse-checkout", "set", f"{sparse_path}/*.xlsx"],
                        cwd=str(local_dir),
                        capture_output=True, text=True, timeout=30, check=True,
                    )
                else:
                    subprocess.run(
                        ["git", "sparse-checkout", "init", "--cone"],
                        cwd=str(local_dir),
                        capture_output=True, text=True, timeout=10, check=True,
                    )
                    subprocess.run(
                        ["git", "sparse-checkout", "set", sparse_path],
                        cwd=str(local_dir),
                        capture_output=True, text=True, timeout=30, check=True,
                    )

                subprocess.run(
                    ["git", "checkout"],
                    cwd=str(local_dir),
                    capture_output=True, text=True, timeout=600, check=True,
                )
                logger.info("│  ✓ val_%s_%s/ ready", mod_lower, suffix)
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr.strip() if exc.stderr else str(exc)
                if "not found" in stderr.lower() or "does not exist" in stderr.lower():
                    _FAILED_REPO_SLUGS.add(repo_slug)
                    logger.debug("│  Repo %s does not exist — skipping", repo_slug)
                    break  # No point trying other suffixes for this repo
                logger.warning("│  ⚠ Failed to clone %s (%s): %s",
                               repo_slug, suffix, stderr)
            except subprocess.TimeoutExpired:
                logger.warning("│  ⚠ Timeout cloning %s (%s)", repo_slug, suffix)


# ---------------------------------------------------------------------------
# BVEC Discovery
# ---------------------------------------------------------------------------

# BVEC filename pattern: TC4xx_SW_MCAL_<Module>_BVEC_Analysis_Report.xlsx
_BVEC_FILE_PATTERN = re.compile(
    r"TC4xx_SW_MCAL_(.+?)_BVEC_Analysis_Report\.xlsx$", re.IGNORECASE
)


def _expand_to_mcal_modules(hw_module: str) -> list[str]:
    """Expand an HW UM module name to candidate MCAL module names.

    Uses MODULE_SUB_MODULES from the main pipeline for multi-repo modules.
    E.g. "ETH" → ["ETH_17_LETH", "ETH_17_GETH"]
         "LETH" → ["LETH", "ETH_17_LETH"]  (adds parent prefix)
         "ADC" → ["ADC"]
    """
    try:
        from dependency_fetcher import MODULE_SUB_MODULES
    except ImportError:
        MODULE_SUB_MODULES = {}

    upper = hw_module.upper()

    # Direct expansion (e.g. "ETH" → ["ETH_17_LETH", "ETH_17_GETH"])
    if upper in MODULE_SUB_MODULES:
        return [sub.upper() for sub in MODULE_SUB_MODULES[upper]]

    # Check if it's already a sub-module name (e.g. "ETH_17_LETH")
    candidates = [upper]

    # Check if it matches the suffix of a known sub-module
    # e.g. "LETH" → matches "ETH_17_LETH"
    for parent, subs in MODULE_SUB_MODULES.items():
        for sub in subs:
            sub_upper = sub.upper()
            if sub_upper.endswith(upper) or upper in sub_upper:
                if sub_upper not in candidates:
                    candidates.append(sub_upper)

    return candidates


def _discover_bvec_file(module: str) -> Optional[tuple[Path, str]]:
    """Discover a BVEC Excel file for the given module.

    Searches multiple locations:
      1. temp/val_<module>_specs/00_Specs/  (manual placement)
      2. temp/temporary_data/aurix3g_sw_mcal_tc4xx_val_<module>/  (pipeline fetch)
      3. temp/ recursive fallback

    Returns:
        Tuple of (xlsx_path, mcal_module_name) or None if not found.
    """
    # Expand HW module name to all candidate MCAL names
    mcal_candidates = _expand_to_mcal_modules(module)

    # Collect all BVEC files from known locations
    bvec_files: list[tuple[Path, str]] = []  # (path, mcal_name_from_filename)

    for mcal_mod in mcal_candidates:
        mod_lower = mcal_mod.lower()

        # Pattern 1: temp/val_<module>_specs/00_Specs/
        specs_dir = TEMP_DIR / f"val_{mod_lower}_specs" / "00_Specs"
        if specs_dir.is_dir():
            for xlsx in specs_dir.glob("*BVEC*.xlsx"):
                match = _BVEC_FILE_PATTERN.match(xlsx.name)
                if match:
                    bvec_files.append((xlsx, match.group(1)))

        # Pattern 2: temp/temporary_data/aurix3g_sw_mcal_tc4xx_val_<module>/
        val_dir = TEMP_DIR / "temporary_data" / f"aurix3g_sw_mcal_tc4xx_val_{mod_lower}"
        if val_dir.is_dir():
            for xlsx in val_dir.rglob("*BVEC*.xlsx"):
                match = _BVEC_FILE_PATTERN.match(xlsx.name)
                if match:
                    bvec_files.append((xlsx, match.group(1)))

    # Fallback: search all val_*_specs directories
    if not bvec_files:
        for val_dir in TEMP_DIR.glob("val_*_specs"):
            specs_dir = val_dir / "00_Specs"
            if specs_dir.is_dir():
                for xlsx in specs_dir.glob("*BVEC*.xlsx"):
                    match = _BVEC_FILE_PATTERN.match(xlsx.name)
                    if match:
                        bvec_files.append((xlsx, match.group(1)))

    if not bvec_files:
        return None

    # Match against candidates
    mod_upper = module.upper()
    for xlsx_path, mcal_name in bvec_files:
        mcal_upper = mcal_name.upper().replace("_", "")
        # Direct match or suffix match
        if mod_upper in mcal_upper or mcal_upper.endswith(mod_upper):
            mcal_module = "_".join(p.upper() for p in mcal_name.split("_"))
            return xlsx_path, mcal_module

    # If candidates were expanded, try matching expanded names against filenames
    for xlsx_path, mcal_name in bvec_files:
        file_mcal = "_".join(p.upper() for p in mcal_name.split("_"))
        if file_mcal in mcal_candidates:
            return xlsx_path, file_mcal

    return None


def step5_bvec(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_bvec: bool = False, project: str = "A3G", auto_fetch_val: bool = False, **_
) -> None:
    """Step 5: Ingest BVEC (Boundary Value & Equivalence Class) analysis data.

    Discovers the BVEC Excel file for the given module in temp/val_*_specs/
    and ingests boundary value analysis nodes + relationships into Neo4j.
    """
    if skip_bvec:
        logger.info("┌─ Step 5: BVEC ingestion (SKIPPED — --skip-bvec)")
        logger.info("└─ Step 5 skipped\n")
        return

    logger.info("┌─ Step 5: BVEC ingestion")

    # Handle multiple modules
    if module.upper() == "ALL":
        logger.info("│  Module=ALL — BVEC discovery not supported for ALL, skipping")
        logger.info("└─ Step 5 skipped (use explicit --module)\n")
        return

    modules = [m.strip() for m in module.split(",")]

    # Auto-fetch val repos if requested
    if auto_fetch_val:
        for mod in modules:
            _ensure_val_repo(mod, dry_run=dry_run)

    found_any = False

    for mod in modules:
        result = _discover_bvec_file(mod)
        if result is None:
            logger.info("│  No BVEC file found for module '%s' — skipping", mod)
            continue

        xlsx_path, mcal_module = result
        found_any = True
        logger.info("│  Module: %s → MCAL: %s", mod, mcal_module)
        logger.info("│  File: %s", xlsx_path.name)

        if dry_run:
            logger.info("│  [DRY-RUN] Would ingest BVEC from %s", xlsx_path.name)
            continue

        storage_cfg = _load_storage_config()
        neo4j_cfg = _get_neo4j_cfg(profile, storage_cfg)

        from bvec_kg_builder import BVECKnowledgeGraphBuilder

        builder = BVECKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            xlsx_path=xlsx_path,
            module=mcal_module,
            project=project,
            dry_run=dry_run,
            clear_module=clear,
        )
        builder.build()

    if not found_any:
        logger.info("│  No BVEC files found for any requested module")

    logger.info("└─ Step 5 complete\n")


# ---------------------------------------------------------------------------
# TD (Test Data) Discovery
# ---------------------------------------------------------------------------

# TD filename pattern: TC4xx_SW_MCAL_TD_<Module>.xlsx
_TD_FILE_PATTERN = re.compile(
    r"TC4xx_SW_MCAL_TD_(.+?)\.xlsx$", re.IGNORECASE
)

# ConfigMap filename pattern: TC4XX_SW_MCAL_ConfigMap_<Module>.xlsx
_CONFIGMAP_FILE_PATTERN = re.compile(
    r"TC4XX_SW_MCAL_ConfigMap_(.+?)\.xlsx$", re.IGNORECASE
)


def _discover_td_file(module: str) -> Optional[tuple[Path, str]]:
    """Discover a Test Data Excel file for the given module.

    Searches multiple locations:
      1. temp/val_<module>_impl/01_Implementation/  (manual sparse checkout)
      2. temp/temporary_data/aurix3g_sw_mcal_tc4xx_val_<module>/01_Implementation/
      3. temp/ recursive fallback

    Returns:
        Tuple of (xlsx_path, mcal_module_name) or None if not found.
    """
    mcal_candidates = _expand_to_mcal_modules(module)

    td_files: list[tuple[Path, str]] = []  # (path, mcal_name_from_filename)

    for mcal_mod in mcal_candidates:
        mod_lower = mcal_mod.lower()

        # Pattern 1: temp/val_<module>_impl/01_Implementation/
        impl_dir = TEMP_DIR / f"val_{mod_lower}_impl" / "01_Implementation"
        if impl_dir.is_dir():
            for xlsx in impl_dir.glob("TC4xx_SW_MCAL_TD_*.xlsx"):
                match = _TD_FILE_PATTERN.match(xlsx.name)
                if match:
                    td_files.append((xlsx, match.group(1)))

        # Pattern 2: temp/temporary_data/aurix3g_sw_mcal_tc4xx_val_<module>/
        val_dir = TEMP_DIR / "temporary_data" / f"aurix3g_sw_mcal_tc4xx_val_{mod_lower}"
        if val_dir.is_dir():
            for xlsx in val_dir.rglob("TC4xx_SW_MCAL_TD_*.xlsx"):
                match = _TD_FILE_PATTERN.match(xlsx.name)
                if match:
                    td_files.append((xlsx, match.group(1)))

        # Pattern 3: temp/val_<module>_specs/ (some repos put TD in specs too)
        specs_dir = TEMP_DIR / f"val_{mod_lower}_specs"
        if specs_dir.is_dir():
            for xlsx in specs_dir.rglob("TC4xx_SW_MCAL_TD_*.xlsx"):
                match = _TD_FILE_PATTERN.match(xlsx.name)
                if match:
                    td_files.append((xlsx, match.group(1)))

    # Fallback: search all val_*_impl directories
    if not td_files:
        for val_dir in TEMP_DIR.glob("val_*_impl"):
            impl_dir = val_dir / "01_Implementation"
            if impl_dir.is_dir():
                for xlsx in impl_dir.glob("TC4xx_SW_MCAL_TD_*.xlsx"):
                    match = _TD_FILE_PATTERN.match(xlsx.name)
                    if match:
                        td_files.append((xlsx, match.group(1)))

    if not td_files:
        return None

    # Match against candidates
    mod_upper = module.upper()
    for xlsx_path, td_name in td_files:
        td_upper = td_name.upper().replace("_", "")
        if mod_upper in td_upper or td_upper.endswith(mod_upper):
            mcal_module = "_".join(p.upper() for p in td_name.split("_"))
            return xlsx_path, mcal_module

    # Try matching expanded names against filenames
    for xlsx_path, td_name in td_files:
        file_mcal = "_".join(p.upper() for p in td_name.split("_"))
        if file_mcal in mcal_candidates:
            return xlsx_path, file_mcal

    return None


def step6_td(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_td: bool = False, project: str = "A3G", auto_fetch_val: bool = False, **_
) -> None:
    """Step 6: Ingest Test Data (TD) parameters + configuration mappings.

    Discovers the TD Excel file for the given module and ingests IO parameter
    values, configurations, HW connections, and interface modes into Neo4j.
    """
    if skip_td:
        logger.info("┌─ Step 6: TD ingestion (SKIPPED — --skip-td)")
        logger.info("└─ Step 6 skipped\n")
        return

    logger.info("┌─ Step 6: TD (Test Data) ingestion")

    if module.upper() == "ALL":
        logger.info("│  Module=ALL — TD discovery not supported for ALL, skipping")
        logger.info("└─ Step 6 skipped (use explicit --module)\n")
        return

    modules = [m.strip() for m in module.split(",")]

    # Auto-fetch val repos if requested (and not already fetched by step 5)
    if auto_fetch_val:
        for mod in modules:
            _ensure_val_repo(mod, dry_run=dry_run)

    found_any = False

    for mod in modules:
        result = _discover_td_file(mod)
        if result is None:
            logger.info("│  No TD file found for module '%s' — skipping", mod)
            continue

        xlsx_path, mcal_module = result
        found_any = True
        logger.info("│  Module: %s → MCAL: %s", mod, mcal_module)
        logger.info("│  File: %s", xlsx_path.name)

        if dry_run:
            logger.info("│  [DRY-RUN] Would ingest TD from %s", xlsx_path.name)
            continue

        storage_cfg = _load_storage_config()
        neo4j_cfg = _get_neo4j_cfg(profile, storage_cfg)

        from td_kg_builder import TDKnowledgeGraphBuilder

        builder = TDKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            xlsx_path=xlsx_path,
            module=mcal_module,
            project=project,
            dry_run=dry_run,
            clear_module=clear,
        )
        builder.build()

    if not found_any:
        logger.info("│  No TD files found for any requested module")

    logger.info("└─ Step 6 complete\n")


# ---------------------------------------------------------------------------
# Step 7 — ConfigMap Ingestion
# ---------------------------------------------------------------------------

def _discover_configmap_file(module: str) -> Optional[tuple[Path, str]]:
    """Discover a ConfigMap Excel file for the given module.

    Searches:
      1. temp/val_<module>_cfg/02_Cfg/  (manual sparse checkout)
      2. temp/temporary_data/aurix3g_sw_mcal_tc4xx_val_<module>/02_Cfg/
      3. temp/ recursive fallback

    Returns:
        Tuple of (xlsx_path, mcal_module_name) or None if not found.
    """
    mcal_candidates = _expand_to_mcal_modules(module)

    cm_files: list[tuple[Path, str]] = []

    for mcal_mod in mcal_candidates:
        mod_lower = mcal_mod.lower()

        # Pattern 1: temp/val_<module>_cfg/02_Cfg/
        cfg_dir = TEMP_DIR / f"val_{mod_lower}_cfg" / "02_Cfg"
        if cfg_dir.is_dir():
            for xlsx in cfg_dir.glob("TC4XX_SW_MCAL_ConfigMap_*.xlsx"):
                match = _CONFIGMAP_FILE_PATTERN.match(xlsx.name)
                if match:
                    cm_files.append((xlsx, match.group(1)))

        # Pattern 2: temp/temporary_data/.../02_Cfg/
        val_dir = TEMP_DIR / "temporary_data" / f"aurix3g_sw_mcal_tc4xx_val_{mod_lower}"
        if val_dir.is_dir():
            for xlsx in val_dir.rglob("TC4XX_SW_MCAL_ConfigMap_*.xlsx"):
                match = _CONFIGMAP_FILE_PATTERN.match(xlsx.name)
                if match:
                    cm_files.append((xlsx, match.group(1)))

        # Pattern 3: temp/val_<module>_impl/ (some repos keep ConfigMap alongside TD)
        impl_dir = TEMP_DIR / f"val_{mod_lower}_impl"
        if impl_dir.is_dir():
            for xlsx in impl_dir.rglob("TC4XX_SW_MCAL_ConfigMap_*.xlsx"):
                match = _CONFIGMAP_FILE_PATTERN.match(xlsx.name)
                if match:
                    cm_files.append((xlsx, match.group(1)))

    # Fallback: search all val_*_cfg directories
    if not cm_files:
        for val_dir in TEMP_DIR.glob("val_*_cfg"):
            cfg_dir = val_dir / "02_Cfg"
            if cfg_dir.is_dir():
                for xlsx in cfg_dir.glob("TC4XX_SW_MCAL_ConfigMap_*.xlsx"):
                    match = _CONFIGMAP_FILE_PATTERN.match(xlsx.name)
                    if match:
                        cm_files.append((xlsx, match.group(1)))

    if not cm_files:
        return None

    # Match against candidates
    mod_upper = module.upper()
    for xlsx_path, cm_name in cm_files:
        cm_upper = cm_name.upper().replace("_", "")
        if mod_upper in cm_upper or cm_upper.endswith(mod_upper):
            mcal_module = "_".join(p.upper() for p in cm_name.split("_"))
            return xlsx_path, mcal_module

    return None


def step7_configmap(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_configmap: bool = False, project: str = "A3G", auto_fetch_val: bool = False, **_
) -> None:
    """Step 7: Ingest ConfigMap device-to-configuration mappings.

    Discovers the ConfigMap Excel file and ingests device variant nodes
    and CM_RUNS_ON_DEVICE / BVEC_TARGETS_DEVICE relationships into Neo4j.
    """
    if skip_configmap:
        logger.info("┌─ Step 7: ConfigMap ingestion (SKIPPED — --skip-configmap)")
        logger.info("└─ Step 7 skipped\n")
        return

    logger.info("┌─ Step 7: ConfigMap (device-to-config mapping) ingestion")

    if module.upper() == "ALL":
        logger.info("│  Module=ALL — ConfigMap discovery not supported for ALL, skipping")
        logger.info("└─ Step 7 skipped (use explicit --module)\n")
        return

    modules = [m.strip() for m in module.split(",")]

    # Auto-fetch val repos if requested (and not already fetched by earlier steps)
    if auto_fetch_val:
        for mod in modules:
            _ensure_val_repo(mod, dry_run=dry_run)

    found_any = False

    for mod in modules:
        result = _discover_configmap_file(mod)
        if result is None:
            logger.info("│  No ConfigMap file found for module '%s' — skipping", mod)
            continue

        xlsx_path, mcal_module = result
        found_any = True
        logger.info("│  Module: %s → MCAL: %s", mod, mcal_module)
        logger.info("│  File: %s", xlsx_path.name)

        if dry_run:
            logger.info("│  [DRY-RUN] Would ingest ConfigMap from %s", xlsx_path.name)
            continue

        storage_cfg = _load_storage_config()
        neo4j_cfg = _get_neo4j_cfg(profile, storage_cfg)

        from configmap_kg_builder import ConfigMapKnowledgeGraphBuilder

        builder = ConfigMapKnowledgeGraphBuilder(
            neo4j_cfg=neo4j_cfg,
            xlsx_path=xlsx_path,
            module=mcal_module,
            project=project,
            dry_run=dry_run,
            clear_module=clear,
        )
        builder.build()

    if not found_any:
        logger.info("│  No ConfigMap files found for any requested module")

    logger.info("└─ Step 7 complete\n")


# ---------------------------------------------------------------------------
# Test Script ingestion (Steps 8 + 9)
# ---------------------------------------------------------------------------

def _resolve_val_impl_dir(module: str) -> Path | None:
    """Return the val impl directory for a module, or None if not found.

    Checks ``temp/val_{module_lower}_impl/`` first,
    then falls back to ``temp/val_{module_lower}/01_Implementation/``.
    """
    mod = module.lower()
    # Primary: manually placed repos
    candidate = TEMP_DIR / f"val_{mod}_impl"
    if candidate.is_dir():
        return candidate
    # Fallback: auto-fetched repos (the impl suffix is already fetched via _ensure_val_repo)
    candidate = TEMP_DIR / f"val_{mod}_impl" / "01_Implementation"
    if candidate.is_dir():
        return candidate.parent
    return None


def step8_testscript_kg(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_testscript: bool = False, project: str = "A3G",
    auto_fetch_val: bool = False, **_
) -> None:
    """Step 8: Ingest test script .c files into Neo4j KG (TSCR nodes).

    Discovers Test_*.c files for the given module in temp/val_*_impl/
    and ingests test case/step nodes into Neo4j.
    """
    if skip_testscript:
        logger.info("┌─ Step 8: Test Script KG ingestion (SKIPPED — --skip-testscript)")
        logger.info("└─ Step 8 skipped\n")
        return

    logger.info("┌─ Step 8: Test Script KG ingestion")

    if module.upper() == "ALL":
        logger.info("│  Module=ALL — test script discovery not supported for ALL, skipping")
        logger.info("└─ Step 8 skipped (use explicit --module)\n")
        return

    modules = [m.strip() for m in module.split(",")]

    # Auto-fetch val repos if requested
    if auto_fetch_val:
        for mod in modules:
            _ensure_val_repo(mod, dry_run=dry_run)

    found_any = False

    for mod in modules:
        # Expand to MCAL sub-modules (e.g. ETH → ETH_17_LETH, ETH_17_GETH)
        mcal_candidates = _expand_to_mcal_modules(mod)

        for sub in mcal_candidates:
            val_dir = _resolve_val_impl_dir(sub)
            if val_dir is None:
                continue

            src_dir = val_dir / "01_Implementation" / "src"
            test_files = sorted(src_dir.glob("Test_*.c")) if src_dir.is_dir() else []
            if not test_files:
                continue

            found_any = True
            logger.info("│  Module: %s — %d test file(s)", sub, len(test_files))

            if dry_run:
                logger.info("│  [DRY-RUN] Would ingest %d Test_*.c files for %s",
                            len(test_files), sub)
                continue

            cmd = [
                PYTHON, "testscript_kg_builder.py",
                *[str(f) for f in test_files],
                "--module", sub,
                "--profile", profile,
                "--project", project,
                "-v",
            ]
            if clear:
                cmd.append("--clear")

            _run(
                cmd,
                cwd=CODE_DIR / "KG",
                label=f"Step 8: KG ingestion (Test Script — {sub})",
                dry_run=False,
            )

    if not found_any:
        logger.info("│  No test script files found for any requested module")

    logger.info("└─ Step 8 complete\n")


def step9_testscript_qdrant(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_testscript: bool = False, project: str = "A3G",
    auto_fetch_val: bool = False, **_
) -> None:
    """Step 9: Ingest test script .c files into Qdrant vector store.

    Discovers Test_*.c files and their headers, then ingests into Qdrant
    for semantic search over test implementations.
    """
    if skip_testscript:
        logger.info("┌─ Step 9: Test Script Qdrant ingestion (SKIPPED — --skip-testscript)")
        logger.info("└─ Step 9 skipped\n")
        return

    logger.info("┌─ Step 9: Test Script Qdrant ingestion")

    if module.upper() == "ALL":
        logger.info("│  Module=ALL — test script discovery not supported for ALL, skipping")
        logger.info("└─ Step 9 skipped (use explicit --module)\n")
        return

    modules = [m.strip() for m in module.split(",")]

    if auto_fetch_val:
        for mod in modules:
            _ensure_val_repo(mod, dry_run=dry_run)

    found_any = False

    for mod in modules:
        mcal_candidates = _expand_to_mcal_modules(mod)

        for sub in mcal_candidates:
            val_dir = _resolve_val_impl_dir(sub)
            if val_dir is None:
                continue

            src_dir = val_dir / "01_Implementation" / "src"
            inc_dir = val_dir / "01_Implementation" / "inc"
            test_files = sorted(src_dir.glob("Test_*.c")) if src_dir.is_dir() else []
            if not test_files:
                continue

            found_any = True
            header_files = sorted(inc_dir.glob("*.h")) if inc_dir.is_dir() else []
            logger.info("│  Module: %s — %d test file(s), %d header(s)",
                        sub, len(test_files), len(header_files))

            if dry_run:
                logger.info("│  [DRY-RUN] Would ingest %d files into Qdrant for %s",
                            len(test_files), sub)
                continue

            # Process each test file separately (Qdrant script takes single --src)
            for test_file in test_files:
                cmd = [
                    PYTHON, "testscript_qdrant_ingest.py",
                    "--src", str(test_file),
                    "--module", sub,
                ]
                if header_files:
                    cmd.append("--headers")
                    cmd.extend([str(h) for h in header_files])

                _run(
                    cmd,
                    cwd=CODE_DIR / "KG",
                    label=f"Step 9: Qdrant ingestion ({test_file.name})",
                    dry_run=False,
                )

    if not found_any:
        logger.info("│  No test script files found for any requested module")

    logger.info("└─ Step 9 complete\n")


# ---------------------------------------------------------------------------
# Datasheet Pin Mux ingestion (Step 10)
# ---------------------------------------------------------------------------

_DATASHEET_DIR = TEMP_DIR / "datasheet"
_DATASHEET_CONFIG = CODE_DIR / "KG" / "datasheet_config.yaml"


def _load_datasheet_config() -> dict | None:
    """Load datasheet_config.yaml with env-var resolution."""
    if not _DATASHEET_CONFIG.exists():
        logger.warning("datasheet_config.yaml not found at %s", _DATASHEET_CONFIG)
        return None
    import yaml
    with open(_DATASHEET_CONFIG, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    hybridrag_root = str(HYBRIDRAG_DIR).replace("\\", "/")

    def _resolve(obj):
        if isinstance(obj, str):
            return obj.replace("${HYBRIDRAG_ROOT}", hybridrag_root)
        if isinstance(obj, dict):
            return {k: _resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_resolve(v) for v in obj]
        return obj

    return _resolve(raw)


def _fetch_datasheet_pdf(pdf_cfg: dict, output_dir: Path) -> Path | None:
    """Locate or fetch a datasheet PDF.

    Priority: 1. local_path  2. Bitbucket shallow-clone
    """
    local = pdf_cfg.get("local_path")
    if local:
        local_path = Path(local).resolve()
        if local_path.exists():
            return local_path

    bb = pdf_cfg.get("bitbucket")
    if bb:
        repo_slug = bb["repo"]
        file_path = bb["path"]
        ref = bb.get("ref", "master")
        project = bb.get("project", "ATVA3GMCAL")

        repo_dir = output_dir / "repos" / repo_slug
        clone_url = f"https://bitbucket.vih.infineon.com/scm/{project}/{repo_slug}.git"

        if (repo_dir / ".git").is_dir():
            pdf_path = repo_dir / file_path
            if pdf_path.exists():
                return pdf_path
            logger.warning("PDF not found in repo at %s", pdf_path)
            return None

        fail_marker = output_dir / "repos" / f".{repo_slug}.failed"
        if fail_marker.exists():
            logger.debug("Skipping %s — previous clone attempt failed", repo_slug)
            return None

        logger.info("│  Cloning datasheet repo %s (ref=%s) …", repo_slug, ref)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", ref,
                 "--single-branch", clone_url, str(repo_dir)],
                capture_output=True, text=True, timeout=300, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("│  Failed to clone datasheet repo %s: %s", repo_slug, exc)
            fail_marker.parent.mkdir(parents=True, exist_ok=True)
            fail_marker.write_text(str(exc), encoding="utf-8")
            return None

        pdf_path = repo_dir / file_path
        if pdf_path.exists():
            return pdf_path
        logger.warning("PDF not found in repo at %s", pdf_path)

    return None


def _ensure_csv_exists(
    device: str,
    device_cfg: dict,
    pdf_path: Path,
    output_dir: Path,
    table_parser: str | None,
    dry_run: bool = False,
) -> Path | None:
    """Ensure a pin mux CSV exists for a device, generating it if needed."""
    csv_name = f"{device}_pin_mux.csv"
    csv_path = output_dir / csv_name

    if csv_path.exists():
        if csv_path.stat().st_mtime >= pdf_path.stat().st_mtime:
            logger.info("│  CSV cached: %s (newer than PDF)", csv_name)
            return csv_path
        logger.info("│  CSV stale: %s (PDF is newer, re-extracting)", csv_name)

    if not table_parser or not Path(table_parser).exists():
        if not csv_path.exists():
            logger.warning("│  table_parser not found — cannot generate %s", csv_name)
            return None
        logger.warning("│  table_parser not found — using existing (stale) CSV: %s", csv_name)
        return csv_path

    page_start = device_cfg.get("page_start")
    page_end = device_cfg.get("page_end")
    if not page_start or not page_end:
        logger.warning("│  No page_start/page_end for device %s — cannot extract", device)
        return csv_path if csv_path.exists() else None

    if dry_run:
        logger.info("│  [DRY-RUN] Would extract %s pages %d–%d → %s",
                    pdf_path.name, page_start, page_end, csv_name)
        return csv_path if csv_path.exists() else None

    logger.info("│  Extracting %s pages %d–%d → %s …",
                pdf_path.name, page_start, page_end, csv_name)
    cmd = [
        PYTHON, str(table_parser),
        str(pdf_path), str(page_start), str(page_end),
        "-o", str(csv_path),
        "--flavor", "lattice",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode != 0:
            logger.error("│  table_parser failed for %s (exit %d): %s",
                         device, result.returncode,
                         result.stderr[:500] if result.stderr else "no stderr")
            return csv_path if csv_path.exists() else None
        logger.info("│  Extracted: %s", csv_name)
        return csv_path
    except subprocess.TimeoutExpired:
        logger.error("│  table_parser timed out for %s (pages %d–%d)",
                     device, page_start, page_end)
        return csv_path if csv_path.exists() else None
    except Exception as exc:
        logger.error("│  table_parser error for %s: %s", device, exc)
        return csv_path if csv_path.exists() else None


def step10_datasheet(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_datasheet: bool = False, project: str = "A3G", **_
) -> None:
    """Step 10: Ingest datasheet pin mux CSV data into Neo4j KG (DS_* nodes).

    Two-phase step:
      Phase 1: Ensure CSVs exist (fetch PDF + run table_parser if needed)
      Phase 2: Ingest each CSV into Neo4j via datasheet_kg_builder.py
    """
    if skip_datasheet:
        logger.info("┌─ Step 10: Datasheet ingestion (SKIPPED — --skip-datasheet)")
        logger.info("└─ Step 10 skipped\n")
        return

    logger.info("┌─ Step 10: Datasheet Pin Mux ingestion")

    datasheet_dir = _DATASHEET_DIR
    datasheet_dir.mkdir(parents=True, exist_ok=True)

    ds_config = _load_datasheet_config()
    table_parser = ds_config.get("table_parser_path") if ds_config else None

    # Phase 1: Ensure CSVs are available
    csv_files: list[tuple[Path, str]] = []  # (csv_path, device_name)

    if ds_config and "datasheets" in ds_config:
        logger.info("│  Phase 1: Ensuring pin mux CSVs are available …")
        datasheets = ds_config["datasheets"]

        for ds_key, ds_cfg in datasheets.items():
            pdf_cfg = ds_cfg.get("pdf", {})
            devices = ds_cfg.get("devices", {})
            if not devices:
                continue

            pdf_path = _fetch_datasheet_pdf(pdf_cfg, datasheet_dir)
            if not pdf_path:
                logger.warning("│  Datasheet '%s': PDF not available — checking pre-existing CSVs",
                               ds_key)
                for device in devices:
                    csv_candidate = datasheet_dir / f"{device}_pin_mux.csv"
                    if csv_candidate.exists():
                        csv_files.append((csv_candidate, device))
                        logger.info("│  Using existing CSV: %s", csv_candidate.name)
                    else:
                        logger.warning("│  No CSV for %s — skipping", device)
                continue

            for device, device_cfg in devices.items():
                csv_path = _ensure_csv_exists(
                    device, device_cfg, pdf_path, datasheet_dir,
                    table_parser, dry_run=dry_run,
                )
                if csv_path and csv_path.exists():
                    csv_files.append((csv_path, device))
                else:
                    logger.warning("│  Device %s: no CSV available — skipping", device)
    else:
        logger.info("│  No datasheet_config.yaml — discovering existing CSVs in %s",
                    datasheet_dir)
        for csv_file in sorted(datasheet_dir.glob("*_pin_mux.csv")):
            device = csv_file.stem.replace("_pin_mux", "")
            csv_files.append((csv_file, device))

    if not csv_files:
        logger.warning("│  No pin mux CSVs available — skipping")
        logger.info("└─ Step 10 skipped (no data)\n")
        return

    # Phase 2: Ingest CSVs into Neo4j
    logger.info("│  Phase 2: Ingesting %d device CSV(s) into Neo4j …", len(csv_files))
    succeeded = 0
    failed = 0

    for csv_path, device in csv_files:
        cmd = [
            PYTHON, "datasheet_kg_builder.py",
            str(csv_path),
            "--device", device,
            "--module", "PORT",
            "--profile", profile,
            "--project", project,
            "-v",
        ]
        if clear:
            cmd.append("--clear")

        if dry_run:
            logger.info("│  [DRY-RUN] Would ingest %s for device %s", csv_path.name, device)
            succeeded += 1
            continue

        label = f"Step 10: DS Pin Mux — {device}"
        try:
            _run(cmd, cwd=CODE_DIR / "KG", label=label, dry_run=False)
            succeeded += 1
        except SystemExit:
            logger.error("│  FAILED: %s — continuing with remaining devices", device)
            failed += 1

    logger.info("│  Result: %d succeeded, %d failed (of %d total)",
                succeeded, failed, len(csv_files))
    logger.info("└─ Step 10 complete\n")


# ---------------------------------------------------------------------------
# ARXML ECUC Configuration Ingestion (Step 11)
# ---------------------------------------------------------------------------

def _ensure_arxml_repo(module: str, dry_run: bool = False) -> Path | None:
    """Clone or reuse the val repo's 02_Cfg/AS460/ directory for ARXML ingestion.

    Clones the val repo with sparse-checkout of '02_Cfg/AS460' into
    temp/val_{module_lower}_arxml/. Returns the AS460 directory path
    or None if the repo is unavailable.
    """
    mcal_candidates = _expand_to_mcal_modules(module)

    for mcal_mod in mcal_candidates:
        mod_lower = mcal_mod.lower()
        local_dir = TEMP_DIR / f"val_{mod_lower}_arxml"
        as460_dir = local_dir / "02_Cfg" / "AS460"

        # Already fetched?
        if as460_dir.is_dir() and any(as460_dir.iterdir()):
            return as460_dir

        repo_slug = _val_repo_slug(mcal_mod)

        if repo_slug in _FAILED_REPO_SLUGS:
            continue

        clone_url = f"{BITBUCKET_BASE_URL}/scm/{BITBUCKET_PROJECT}/{repo_slug}.git"

        logger.info("│  Fetching ARXML configs from %s …", repo_slug)
        if dry_run:
            logger.info("│  [DRY-RUN] Would clone %s (sparse: 02_Cfg/AS460)", repo_slug)
            return None

        try:
            local_dir.mkdir(parents=True, exist_ok=True)

            # Blobless clone + sparse checkout of 02_Cfg/AS460
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--no-checkout",
                 "--depth", "1", clone_url, str(local_dir)],
                capture_output=True, text=True, timeout=300, check=True,
            )
            subprocess.run(
                ["git", "sparse-checkout", "init", "--cone"],
                cwd=str(local_dir),
                capture_output=True, text=True, timeout=10, check=True,
            )
            subprocess.run(
                ["git", "sparse-checkout", "set", "02_Cfg/AS460"],
                cwd=str(local_dir),
                capture_output=True, text=True, timeout=30, check=True,
            )
            subprocess.run(
                ["git", "checkout"],
                cwd=str(local_dir),
                capture_output=True, text=True, timeout=600, check=True,
            )

            if as460_dir.is_dir():
                logger.info("│  ✓ ARXML repo ready: %s", as460_dir)
                return as460_dir
            else:
                logger.warning("│  02_Cfg/AS460/ not found in %s", repo_slug)
                return None

        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else str(exc)
            if "not found" in stderr.lower() or "does not exist" in stderr.lower():
                _FAILED_REPO_SLUGS.add(repo_slug)
                logger.debug("│  Repo %s does not exist — skipping", repo_slug)
            else:
                logger.warning("│  ⚠ Failed to clone %s: %s", repo_slug, stderr)
            return None
        except subprocess.TimeoutExpired:
            logger.warning("│  ⚠ Timeout cloning %s", repo_slug)
            return None

    return None


def _discover_arxml_devices(as460_dir: Path) -> list[tuple[str, Path]]:
    """Discover all device directories under 02_Cfg/AS460/.

    Returns list of (device_name, device_dir_path) tuples.
    """
    devices = []
    if not as460_dir.is_dir():
        return devices

    for entry in sorted(as460_dir.iterdir()):
        if entry.is_dir() and any(entry.glob("*.arxml")):
            devices.append((entry.name, entry))

    return devices


def step11_arxml(
    module: str, profile: str, clear: bool = False, dry_run: bool = False,
    skip_arxml: bool = False, project: str = "A3G",
    auto_fetch_val: bool = False, **_
) -> None:
    """Step 11: Ingest ARXML ECUC configuration files into Neo4j KG.

    For the given module, discovers the val repo's 02_Cfg/AS460/ directory,
    finds ALL device subdirectories, and ingests ALL .arxml files from each
    device (regardless of ECUC module name in the arxml).

    Creates ARXML_Module, ARXML_Container, ARXML_Parameter, ARXML_Reference
    nodes and cross-links to EA_ConfigParameter, BVEC_ConfigParameter, and
    TD_Configuration.
    """
    if skip_arxml:
        logger.info("┌─ Step 11: ARXML ECUC ingestion (SKIPPED — --skip-arxml)")
        logger.info("└─ Step 11 skipped\n")
        return

    logger.info("┌─ Step 11: ARXML ECUC Configuration ingestion")

    if module.upper() == "ALL":
        logger.info("│  Module=ALL — ARXML discovery not supported for ALL, skipping")
        logger.info("└─ Step 11 skipped (use explicit --module)\n")
        return

    modules = [m.strip() for m in module.split(",")]

    storage_cfg = _load_storage_config()
    neo4j_cfg = _get_neo4j_cfg(profile, storage_cfg)

    found_any = False

    for mod in modules:
        mcal_candidates = _expand_to_mcal_modules(mod)

        for mcal_mod in mcal_candidates:
            mod_lower = mcal_mod.lower()

            # Try to find the AS460 directory (check existing repos first)
            as460_dir = None

            # Check temp/val_{mod}_arxml/02_Cfg/AS460/
            candidate = TEMP_DIR / f"val_{mod_lower}_arxml" / "02_Cfg" / "AS460"
            if candidate.is_dir() and any(candidate.iterdir()):
                as460_dir = candidate

            # Check temp/arxml/repos/aurix3g_sw_mcal_tc4xx_val_{mod}/02_Cfg/AS460/
            if not as460_dir:
                repo_slug = _val_repo_slug(mcal_mod)
                candidate = TEMP_DIR / "arxml" / "repos" / repo_slug / "02_Cfg" / "AS460"
                if candidate.is_dir() and any(candidate.iterdir()):
                    as460_dir = candidate

            # Auto-fetch if not present and requested
            if not as460_dir and auto_fetch_val:
                as460_dir = _ensure_arxml_repo(mod, dry_run=dry_run)

            if not as460_dir:
                logger.info("│  No ARXML data found for %s (use --auto-fetch-val)", mcal_mod)
                continue

            # Discover device directories
            devices = _discover_arxml_devices(as460_dir)
            if not devices:
                logger.info("│  No device directories with .arxml files in %s", as460_dir)
                continue

            found_any = True
            logger.info("│  Module: %s — %d device(s): %s",
                        mcal_mod, len(devices), ", ".join(d[0] for d in devices))

            for device_name, device_dir in devices:
                arxml_count = len(list(device_dir.glob("*.arxml")))
                logger.info("│  → %s: %d .arxml files", device_name, arxml_count)

                if dry_run:
                    logger.info("│    [DRY-RUN] Would ingest %d files for %s/%s",
                                arxml_count, mcal_mod, device_name)
                    continue

                from arxml_kg_builder import ArxmlKGBuilder

                builder = ArxmlKGBuilder(
                    neo4j_cfg=neo4j_cfg,
                    arxml_dir=device_dir,
                    device=device_name,
                    module=mcal_mod,
                    project=project,
                    dry_run=dry_run,
                    clear_device=clear,
                    cross_link=True,
                )
                builder.build()

    if not found_any:
        logger.info("│  No ARXML data found for any requested module")

    logger.info("└─ Step 11 complete\n")


# ---------------------------------------------------------------------------
# Step Registry
# ---------------------------------------------------------------------------
STEPS: dict[int, dict] = {
    0: {"fn": step0_token,           "label": "Refresh LLM token",               "needs_module": False},
    1: {"fn": step1_fetch_repo,      "label": "Fetch HW UM repo from Bitbucket", "needs_module": False},
    2: {"fn": step2_parse_reqif,     "label": "Parse/validate ReqIF module",     "needs_module": True},
    3: {"fn": step3_describe_images, "label": "LLM vision image description",    "needs_module": True},
    4: {"fn": step4_ingest,          "label": "Ingest into Neo4j",               "needs_module": True},
    5: {"fn": step5_bvec,            "label": "BVEC analysis ingestion",         "needs_module": True},
    6: {"fn": step6_td,              "label": "TD (Test Data) ingestion",        "needs_module": True},
    7: {"fn": step7_configmap,       "label": "ConfigMap device mapping",        "needs_module": True},
    8: {"fn": step8_testscript_kg,   "label": "Test Script KG ingestion",        "needs_module": True},
    9: {"fn": step9_testscript_qdrant, "label": "Test Script Qdrant ingestion",  "needs_module": True},
    10: {"fn": step10_datasheet,     "label": "Datasheet Pin Mux ingestion",     "needs_module": True},
    11: {"fn": step11_arxml,         "label": "ARXML ECUC Config ingestion",     "needs_module": True},
}


# ---------------------------------------------------------------------------
# Main Pipeline Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(args: argparse.Namespace):
    """Execute the HW UM ingestion pipeline."""
    t0 = time.time()

    # Handle --list-modules (no profile needed)
    if args.list_modules:
        _list_modules(args.device)
        return

    # Handle --list-devices (no profile needed)
    if args.list_devices:
        _list_devices()
        return

    # Resolve profile
    profile = args.profile
    if not profile:
        profile = _ask_profile()

    # Resolve devices
    devices = _resolve_devices(args.device)

    # Determine which steps to run
    all_steps = sorted(STEPS.keys())

    if args.only:
        steps_to_run = [int(s) for s in args.only.split(",")]
    else:
        steps_to_run = [s for s in all_steps if s >= args.start_from]

    if args.skip_token and 0 in steps_to_run:
        steps_to_run.remove(0)
    if args.skip_fetch and 1 in steps_to_run:
        steps_to_run.remove(1)

    # Print plan
    device_summary = ", ".join(f"{d.device} ({d.version_display})" for d in devices)
    print("\n" + "=" * 64)
    print(f"  HW User Manual Pipeline — Module: {args.module}")
    print(f"  Devices: {device_summary}")
    print(f"  Profile: {profile}  |  Project: {args.project}  |  Steps: {steps_to_run}")
    if args.dry_run:
        print(f"  *** DRY RUN — no writes ***")
    if args.clear:
        print(f"  *** CLEAR — will delete existing HW nodes first ***")
    print("=" * 64 + "\n")

    for step_num in steps_to_run:
        print(f"  [{step_num}] {STEPS[step_num]['label']}")
    print()

    # Build common kwargs
    kwargs = dict(
        module=args.module,
        devices=devices,
        profile=profile,
        project=args.project,
        dry_run=args.dry_run,
        clear=args.clear,
        no_images=args.no_images,
        skip_bvec=args.skip_bvec,
        skip_td=args.skip_td,
        skip_configmap=args.skip_configmap,
        skip_testscript=args.skip_testscript,
        skip_datasheet=args.skip_datasheet,
        skip_arxml=args.skip_arxml,
        auto_fetch_val=args.auto_fetch_val,
        ref=args.ref,
    )

    # Execute steps
    for step_num in steps_to_run:
        step_info = STEPS[step_num]
        step_fn = step_info["fn"]
        step_fn(**kwargs)

    elapsed = time.time() - t0
    print(f"\n{'=' * 64}")
    print(f"  Pipeline complete — {elapsed:.1f}s total")
    print(f"  Module: {args.module}  |  Devices: {len(devices)}  |  Profile: {profile}")
    print(f"{'=' * 64}\n")


def _list_modules(device_arg: str):
    """List all discoverable modules in the ReqIF file(s)."""
    # Use git-tree discovery, fall back to filesystem
    available = discover_device_reqifz_from_git()
    if not available:
        available = discover_device_reqifz()
    if not available:
        print(f"\n  ERROR: No ReqIF files found. Run with --auto-fetch first.\n")
        sys.exit(1)

    from reqif_parser import ReqIFParser

    if device_arg.upper() == "ALL":
        targets = sorted(available.values(), key=lambda d: d.device)
    elif device_arg in available:
        targets = [available[device_arg]]
    else:
        print(f"\n  ERROR: Device '{device_arg}' not found.")
        print(f"  Available: {', '.join(sorted(available.keys()))}\n")
        sys.exit(1)

    for dev in targets:
        if not dev.path.exists():
            print(f"\n  {dev.device} ({dev.version_display}): file not downloaded (use --auto-fetch)")
            continue

        print(f"\n  Loading {dev.device} ({dev.version_display}): {dev.filename}…")
        parser = ReqIFParser(dev.path)
        parser.load()
        modules = parser.get_module_names()
        prefixes = parser.get_module_prefixes()

        print(f"  Discovered {len(modules)} modules:\n")
        print(f"  {'Module':<16s} {'Prefix':<14s}")
        print(f"  {'─' * 16} {'─' * 14}")
        for name in sorted(modules):
            prefix = prefixes.get(name, "?")
            print(f"  {name:<16s} {prefix:<14s}")
    print()


def _list_devices():
    """List all discovered device families and their latest ReqIF versions."""
    # Use git-tree discovery to show all available (even if not downloaded)
    available = discover_device_reqifz_from_git()
    if not available:
        # Fall back to filesystem
        available = discover_device_reqifz()
    if not available:
        print(f"\n  ERROR: No ReqIF files found. Clone the repo first (--auto-fetch).\n")
        sys.exit(1)

    print(f"\n  Discovered {len(available)} device families (latest version each):\n")
    print(f"  {'Device':<10s} {'Version':<10s} {'Downloaded':<12s} {'Filename'}")
    print(f"  {'─' * 10} {'─' * 10} {'─' * 12} {'─' * 50}")
    for device in sorted(available.keys()):
        dev = available[device]
        exists = "✓" if dev.path.exists() else "✗ (fetch)"
        print(f"  {dev.device:<10s} {dev.version_display:<10s} {exists:<12s} {dev.filename}")
    print(f"\n  Use --device TC44x,TC46x to target specific devices.")
    print(f"  Use --device ALL to process all devices.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hardware User Manual → Neo4j Knowledge Graph Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_hw_um_pipeline.py --module GPT12 --profile local\n"
            "  python run_hw_um_pipeline.py --module GPT12 --device ALL --profile local\n"
            "  python run_hw_um_pipeline.py --module ADC --device TC44x,TC46x --profile mcal --clear\n"
            "  python run_hw_um_pipeline.py --module ALL --device TC44x --profile test\n"
            "  python run_hw_um_pipeline.py --module GPT12 --auto-fetch --profile test\n"
            "  python run_hw_um_pipeline.py --module GPT12,ADC --only 4,5 --profile mcal\n"
            "  python run_hw_um_pipeline.py --list-modules\n"
            "  python run_hw_um_pipeline.py --list-devices\n"
            "  python run_hw_um_pipeline.py --module GPT12 --dry-run --profile local\n"
        ),
    )
    parser.add_argument(
        "--module", "-m",
        default="GPT12",
        help="Module name(s) to process (comma-separated, or 'ALL'). Default: GPT12"
    )
    parser.add_argument(
        "--device", "-d",
        default="ALL",
        help=(
            "Device family (e.g. 'TC44x', 'TC46x,TC48x', or 'ALL'). "
            "Default: ALL (process all discovered devices)"
        ),
    )
    parser.add_argument(
        "--profile", "-p",
        choices=sorted(VALID_PROFILES),
        default=None,
        help="Neo4j profile (test/mcal/illd/local). Interactive if omitted."
    )
    parser.add_argument(
        "--start-from", type=int, default=0, metavar="N",
        help="Start from step N (skip earlier steps). Default: 0."
    )
    parser.add_argument(
        "--only", type=str, default=None, metavar="2,3,4",
        help="Run only these steps (comma-separated). Overrides --start-from."
    )
    parser.add_argument(
        "--skip-token",
        action="store_true",
        help="Skip Step 0 (token refresh)."
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip Step 1 (repo fetch). Use if repo is already cloned."
    )
    parser.add_argument(
        "--auto-fetch",
        action="store_true",
        help="Force fetch/update of HW UM repo from Bitbucket."
    )
    parser.add_argument(
        "--auto-fetch-val",
        action="store_true",
        help="Auto-fetch val repos (BVEC/TD/ConfigMap) from Bitbucket if not present locally."
    )
    parser.add_argument(
        "--ref", type=str, default="master",
        help="Git ref (branch/tag) to clone/fetch. Default: master."
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip LLM vision image description (faster, no API calls)."
    )
    parser.add_argument(
        "--skip-bvec",
        action="store_true",
        help="Skip Step 5 (BVEC analysis ingestion)."
    )
    parser.add_argument(
        "--skip-td",
        action="store_true",
        help="Skip Step 6 (TD Test Data ingestion)."
    )
    parser.add_argument(
        "--skip-configmap",
        action="store_true",
        help="Skip Step 7 (ConfigMap device mapping ingestion)."
    )
    parser.add_argument(
        "--skip-testscript",
        action="store_true",
        help="Skip Steps 8-9 (Test Script KG + Qdrant ingestion)."
    )
    parser.add_argument(
        "--skip-datasheet",
        action="store_true",
        help="Skip Step 10 (Datasheet Pin Mux ingestion)."
    )
    parser.add_argument(
        "--skip-arxml",
        action="store_true",
        help="Skip Step 11 (ARXML ECUC Config ingestion)."
    )
    parser.add_argument(
        "--project",
        default="A3G",
        help="Project identifier stamped on all nodes (e.g. 'A3G', 'RC1'). Default: A3G."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and describe images but don't write to Neo4j."
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear existing HW nodes for this module before ingesting."
    )
    parser.add_argument(
        "--list-modules",
        action="store_true",
        help="List all discoverable modules in the ReqIF and exit."
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List all discovered device families with latest versions and exit."
    )

    args = parser.parse_args()

    # Auto-fetch implies step 1 should run
    if args.auto_fetch and args.skip_fetch:
        print("  WARNING: --auto-fetch and --skip-fetch are contradictory. Using --auto-fetch.")
        args.skip_fetch = False

    # If repo doesn't exist and no explicit skip, auto-enable fetch
    if not HW_UM_REPO_DIR.exists() and not args.skip_fetch and not args.list_modules and not args.list_devices:
        args.auto_fetch = True
        logger.info("HW UM repo not found — enabling auto-fetch")

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    run_pipeline(args)


if __name__ == "__main__":
    main()
