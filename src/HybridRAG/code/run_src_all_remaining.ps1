#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Re-ingest source code KG for all modules EXCEPT ADC, DMA, GTM (already done).

.DESCRIPTION
    Runs step 6 (Source Code → Neo4j) of the main pipeline for all remaining
    modules to apply the latest fixes (callee propagation dedup, array-type
    unwrap, struct chain misalignment checks).

.PARAMETER Profile
    Neo4j profile to target (mcal, test, local, illd). Default: mcal

.PARAMETER Project
    Project identifier stamped on all nodes. Default: A3G

.PARAMETER Force
    Force full re-ingestion (bypass incremental hash checks).

.PARAMETER DryRun
    Print commands without executing them.

.PARAMETER Verbose
    Enable DEBUG logging for pipeline output.

.PARAMETER AutoFetch
    Auto-clone source repos from Bitbucket if not present locally.

.EXAMPLE
    .\run_src_all_remaining.ps1 -Force
    .\run_src_all_remaining.ps1 -Force -AutoFetch
#>
param(
    [ValidateSet("mcal", "test", "local", "illd")]
    [string]$Profile = "mcal",
    [string]$Project = "A3G",
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Verbose,
    [switch]$AutoFetch
)

$ErrorActionPreference = "Stop"

# All modules EXCEPT ADC, DMA, GTM (already re-ingested)
$MODULES = @(
    "BMC",
    "CAN_17_MCMCAN",
    "CDSP",
    "DIO",
    "DRE",
    "DSADC",
    "ENCODER",
    "ETH_17_GETH",
    "ETH_17_LETH",
    "FR_17_ERAY",
    "GPT",
    "HSSL",
    "I2C",
    "ICU",
    "LIN_17_ASCLIN",
    "MCALUTIL",
    "MCU",
    "MEM_17_NVM",
    "OCU",
    "PORT",
    "PWM_17_TIMERIP",
    "SENT",
    "SMU",
    "SPI",
    "STM",
    "UART",
    "WDG_17_WTU"
)

$env:PYTHONIOENCODING = "utf-8"

Write-Host ""
Write-Host ("=" * 64)
Write-Host "  Source Code KG Ingestion — All Remaining Modules"
Write-Host "  Profile: $Profile  |  Project: $Project"
Write-Host "  Modules: $($MODULES.Count) total"
Write-Host "  Flags: Force=$Force DryRun=$DryRun Verbose=$Verbose AutoFetch=$AutoFetch"
Write-Host ("=" * 64)
Write-Host ""

$totalStart = Get-Date
$failed = @()
$completed = 0

foreach ($mod in $MODULES) {
    $completed++
    Write-Host "─── [$completed/$($MODULES.Count)] Starting: $mod ───" -ForegroundColor Cyan

    $cmdArgs = @(
        "src/HybridRAG/code/run_pipeline.py",
        "--module", $mod,
        "--only", "6",
        "--profile", $Profile,
        "--project", $Project,
        "--skip-token"
    )

    if ($Force)     { $cmdArgs += "--force" }
    if ($DryRun)    { $cmdArgs += "--dry-run" }
    if ($Verbose)   { $cmdArgs += "--verbose" }
    if ($AutoFetch) { $cmdArgs += "--auto-fetch" }

    $stepStart = Get-Date
    & python @cmdArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  FAILED: $mod (exit code $LASTEXITCODE)" -ForegroundColor Red
        $failed += $mod
    } else {
        $elapsed = ((Get-Date) - $stepStart).TotalSeconds
        Write-Host "  DONE: $mod ($([math]::Round($elapsed, 1))s)" -ForegroundColor Green
    }
    Write-Host ""
}

$totalElapsed = ((Get-Date) - $totalStart).TotalSeconds

Write-Host ("=" * 64)
if ($failed.Count -eq 0) {
    Write-Host "  ALL $($MODULES.Count) MODULES INGESTED SUCCESSFULLY ($([math]::Round($totalElapsed, 1))s)" -ForegroundColor Green
} else {
    Write-Host "  COMPLETED WITH FAILURES ($([math]::Round($totalElapsed, 1))s)" -ForegroundColor Red
    Write-Host "  Failed ($($failed.Count)): $($failed -join ', ')" -ForegroundColor Red
    Write-Host "  Succeeded: $($MODULES.Count - $failed.Count)/$($MODULES.Count)" -ForegroundColor Yellow
    exit 1
}
Write-Host ("=" * 64)
