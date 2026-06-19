"""
Batch ingestion wrapper: runs illd_run_pipeline for all modules iteratively.
Skips HW PDF and PlantUML. Uses --clear to clean module data before re-ingestion.
Saves intermediary files for each module.

Usage:
    python run_all_modules.py
"""
import subprocess
import sys
import time

PYTHON = r"c:\users\ayubkhan\appdata\local\programs\python\python313\python.exe"
PIPELINE = "illd_run_pipeline.py"

# All iLLD modules (excluding LIN which is already ingested)
MODULES = [
    "LPBTM",
    "LPCAN",
    "MXAES",
    "NVMR",
    "PMS",
    "PORTS",
    "PSAR",
    "PSI5",
    "PSI5S",
    "RAMC",
    "RESETSC",
    "RNG",
    "SCB",
    "SCU",
    "SDMMC",
    "SENT",
    "SMU",
    "SPU",
    "TINFRA",
    "WCAN",
    "XDMA",
    "XSPI",
]

def run_module(module: str) -> dict:
    """Run pipeline for a single module. Returns status dict."""
    print(f"\n{'='*70}")
    print(f"  STARTING MODULE: {module}")
    print(f"{'='*70}", flush=True)
    
    cmd = [
        PYTHON, "-u", PIPELINE,
        "--module", module,
        "--remote",
        "--clear",
        "--skip-hw",
        "--skip-puml",
        "--save-intermediary",
    ]
    
    t0 = time.time()
    # Stream output in real-time
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    
    for line in process.stdout:
        print(f"  {line}", end="", flush=True)
    
    process.wait()
    elapsed = time.time() - t0
    
    success = process.returncode == 0
    status = "SUCCESS" if success else "FAILED"
    
    print(f"\n  [{status}] {module} — {elapsed:.1f}s (exit code {process.returncode})", flush=True)
    
    return {
        "module": module,
        "success": success,
        "elapsed": elapsed,
        "returncode": process.returncode,
    }


def main():
    print("=" * 70)
    print("  BATCH INGESTION — ALL MODULES (skip HW PDF + PlantUML)")
    print(f"  Modules: {len(MODULES)}")
    print(f"  Mode: --clear + --remote + --save-intermediary")
    print("=" * 70)
    
    t_total = time.time()
    results = []
    
    for module in MODULES:
        res = run_module(module)
        results.append(res)
    
    total_elapsed = time.time() - t_total
    
    # Summary
    print("\n\n" + "=" * 70)
    print("  BATCH INGESTION SUMMARY")
    print("=" * 70)
    print(f"  {'Module':<12} {'Status':<10} {'Time':>8}")
    print(f"  {'-'*32}")
    
    succeeded = 0
    failed = 0
    for r in results:
        status = "OK" if r["success"] else "FAILED"
        print(f"  {r['module']:<12} {status:<10} {r['elapsed']:>7.1f}s")
        if r["success"]:
            succeeded += 1
        else:
            failed += 1
    
    print(f"  {'-'*32}")
    print(f"  Total: {succeeded} succeeded, {failed} failed, {total_elapsed:.1f}s")
    print("=" * 70)
    
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
