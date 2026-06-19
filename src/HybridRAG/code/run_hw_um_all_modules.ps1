#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Ingest ALL HW UM modules (18 unique ReqIF modules) into Neo4j MCAL profile.
    
.DESCRIPTION
    Runs the HW UM pipeline for all 18 unique ReqIF modules across all 9 devices.
    Modules missing from specific devices are gracefully skipped by the pipeline.
    
    Unique ReqIF modules (mapped from 21 MCAL drivers):
      ADC       ← Adc
      CANXL     ← Can_17_McmCan
      DMA       ← Dma
      DRE       ← Dre
      FLEXRAY32 ← Fr_17_Eray
      GETH      ← Eth_17_Geth  (4/9 devices only)
      GPT12     ← Gpt          (7/9 devices only)
      GTM       ← Gtm          (8/9 devices only)
      HSSL      ← Hssl         (8/9 devices only)
      INT       ← Irq
      LETH      ← Eth_17_Leth
      LIN       ← Lin_17_Asclin, I2c, Uart
      PORTX     ← Port, Dio
      SCU       ← Mcu
      SENT      ← Sent         (8/9 devices only)
      SMU       ← Smu
      SPI       ← Spi
      WTU       ← Wdg_17_Wtu

.PARAMETER Profile
    Neo4j profile to target (mcal, test, local). Default: mcal

.PARAMETER Project
    Project identifier stamped on all nodes (A3G, RC1). Default: A3G

.PARAMETER DryRun
    Parse only, don't write to Neo4j.

.PARAMETER NoImages
    Skip LLM vision image description.

.PARAMETER Clear
    Clear existing HW nodes before ingesting.

.PARAMETER SkipFetch
    Skip git fetch for HW UM repo (use existing local repo).

.PARAMETER AutoFetchVal
    Auto-fetch val repos (BVEC/TD/ConfigMap) from Bitbucket if not present locally.

.PARAMETER Only
    Run only specific steps (comma-separated). E.g. "5,6,7" for test artefacts only.

.EXAMPLE
    .\run_hw_um_all_modules.ps1 -Profile mcal -SkipFetch -AutoFetchVal
    .\run_hw_um_all_modules.ps1 -Profile test -DryRun
    .\run_hw_um_all_modules.ps1 -Profile mcal -NoImages -SkipFetch -Only "5,6,7"
    .\run_hw_um_all_modules.ps1 -Profile mcal -Project RC1 -AutoFetchVal -Only "5,6,7"
#>

param(
    [ValidateSet("mcal", "test", "local", "illd")]
    [string]$Profile = "mcal",
    [string]$Project = "A3G",
    [switch]$DryRun,
    [switch]$NoImages,
    [switch]$Clear,
    [switch]$SkipFetch,
    [switch]$AutoFetchVal,
    [string]$Only = ""
)

$ErrorActionPreference = "Stop"

# All 18 unique ReqIF HW UM modules
$ALL_MODULES = @(
    "ADC",
    "CANXL",
    "DMA",
    "DRE",
    "FLEXRAY32",
    "GETH",
    "GPT12",
    "GTM",
    "HSSL",
    "INT",
    "LETH",
    "LIN",
    "PORTX",
    "SCU",
    "SENT",
    "SMU",
    "SPI",
    "WTU"
)

$moduleList = $ALL_MODULES -join ","

# Resolve paths from script location
$REPO_ROOT = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$HW_PIPELINE_SCRIPT = Join-Path $REPO_ROOT "src\HybridRAG\code\run_hw_um_pipeline.py"

if (-not (Test-Path $HW_PIPELINE_SCRIPT)) {
    Write-Host "  ERROR: run_hw_um_pipeline.py not found at: $HW_PIPELINE_SCRIPT" -ForegroundColor Red
    exit 1
}

# Build command args
$cmdArgs = @(
    $HW_PIPELINE_SCRIPT,
    "--module", $moduleList,
    "--device", "ALL",
    "--profile", $Profile,
    "--project", $Project
)

if ($SkipFetch)    { $cmdArgs += "--skip-fetch" }
if ($DryRun)       { $cmdArgs += "--dry-run" }
if ($NoImages)     { $cmdArgs += "--no-images" }
if ($Clear)        { $cmdArgs += "--clear" }
if ($AutoFetchVal) { $cmdArgs += "--auto-fetch-val" }
if ($Only)         { $cmdArgs += "--only"; $cmdArgs += $Only }

# Always skip token refresh if running from activated venv
$cmdArgs += "--skip-token"

Write-Host ""
Write-Host ("=" * 64)
Write-Host "  HW UM Full Ingestion — All 18 Modules × All Devices"
Write-Host "  Profile: $Profile  |  Project: $Project"
Write-Host "  Modules: $moduleList"
Write-Host "  Flags: DryRun=$DryRun NoImages=$NoImages Clear=$Clear SkipFetch=$SkipFetch AutoFetchVal=$AutoFetchVal"
if ($Only) { Write-Host "  Only steps: $Only" }
Write-Host ("=" * 64)
Write-Host ""

$env:PYTHONIOENCODING = "utf-8"
& python @cmdArgs

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n  PIPELINE FAILED with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n  ALL MODULES INGESTED SUCCESSFULLY" -ForegroundColor Green
