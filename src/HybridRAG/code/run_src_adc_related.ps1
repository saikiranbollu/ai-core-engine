#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Re-ingest source code KG for ADC and its closely related modules.

.DESCRIPTION
    Runs step 6 (Source Code → Neo4j) of the main pipeline for ADC and modules
    that ADC directly depends on (DMA for result handling, GTM for timer triggers).

    This is a focused ingestion for DaFA analysis — not a full pipeline run.
    SFR_Register nodes are assumed to already exist in the target database.

    Modules:
      ADC  — Primary ADC driver
      DMA  — DMA controller (ADC DMA result handling)
      GTM  — Generic Timer Module (ADC HW trigger timing)

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
    .\run_src_adc_related.ps1
    .\run_src_adc_related.ps1 -Force
    .\run_src_adc_related.ps1 -Profile test -DryRun
    .\run_src_adc_related.ps1 -Force -Verbose -AutoFetch
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

# ADC + closely related modules (DMA for result handling, GTM for HW triggers)
$MODULES = @("ADC", "DMA", "GTM")

$env:PYTHONIOENCODING = "utf-8"

Write-Host ""
Write-Host ("=" * 64)
Write-Host "  Source Code KG Ingestion — ADC + Related Modules"
Write-Host "  Profile: $Profile  |  Project: $Project"
Write-Host "  Modules: $($MODULES -join ', ')"
Write-Host "  Flags: Force=$Force DryRun=$DryRun Verbose=$Verbose AutoFetch=$AutoFetch"
Write-Host ("=" * 64)
Write-Host ""

$totalStart = Get-Date
$failed = @()

foreach ($mod in $MODULES) {
    Write-Host "─── Starting: $mod ───" -ForegroundColor Cyan

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
    Write-Host "  ALL MODULES INGESTED SUCCESSFULLY ($([math]::Round($totalElapsed, 1))s)" -ForegroundColor Green
} else {
    Write-Host "  COMPLETED WITH FAILURES ($([math]::Round($totalElapsed, 1))s)" -ForegroundColor Red
    Write-Host "  Failed: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host ("=" * 64)
