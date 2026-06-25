#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the full MCAL HybridRAG pipeline (requirements → EA → test spec → source code → SFR → Qdrant)
    for all 30 A3G MCAL modules.

.DESCRIPTION
    Iterates over all 30 MCAL modules and runs run_pipeline.py for each one.
    The pipeline steps are:

      Step 0: Refresh LLM token (IFX SSO)
      Step 1: Fetch Jama SHRQ + PRQ requirements
      Step 2: Fetch Jama relationships
      Step 3: Build base KG (Jama → Neo4j)
      Step 4: KG ingestion (EA model → Neo4j) — requires QEAX file
      Step 5: KG ingestion (Test Spec → Neo4j)
      Step 6: KG ingestion (Source Code → Neo4j)
      Step 7: KG ingestion (SFR registers → Neo4j)
      Step 8: Qdrant ingestion (Source Code → vector DB)

    Each module is processed independently. Failures on one module do not
    prevent subsequent modules from running (unless -StopOnError is set).

    PREREQUISITES:
      - Python venv activated (ai-core-engine/.venv)
      - Jama credentials configured in env/.env (IFX_USERNAME, IFX_PASSWORD)
      - QEAX file accessible (for step 4)
      - Neo4j target instance accessible
      - Qdrant instance accessible (for step 8)
      - Source repos in temp/temporary_data/ OR use -AutoFetch

.PARAMETER Profile
    Neo4j profile to target. Determines which Neo4j instance receives the data.
    Options: mcal (production), test (test instance), illd (ILLD), local (localhost).
    Default: mcal

.PARAMETER Project
    Project identifier stamped on all KG nodes. Used to distinguish data sets.
    Default: A3G

.PARAMETER Only
    Comma-separated list of step numbers to run. Overrides default (all steps).
    Example: "4,5,6,7,8" to skip Jama steps and only do EA + test + source + SFR + Qdrant.

.PARAMETER StartFrom
    Start from this step number (skip earlier steps). Ignored if -Only is set.
    Default: 0 (run all steps).

.PARAMETER QeaxPath
    Path to the Enterprise Architect QEAX model file (required for step 4).
    Default: C:\Users\NairSurajRet\Downloads\2.20.0_tc4xx_sw_mcal\2.20.0_tc4xx_sw_mcal.qeax

.PARAMETER Modules
    Comma-separated list of modules to process. Use to run a subset instead of all 30.
    Example: "ADC,DMA,SPI" to run only those three.
    Default: all 30 modules.

.PARAMETER Force
    Force full re-ingestion for KG steps (bypass incremental hash checks).
    Without this, unchanged source files are skipped.

.PARAMETER ForceJama
    Force re-fetch of Jama requirements even if JSON files already exist.
    Useful when Jama content has been updated upstream.

.PARAMETER Clear
    Clean slate: wipe existing Neo4j data for each module before rebuilding.
    Implies -ForceJama. Use with caution in production.

.PARAMETER AutoFetch
    Auto-clone source/arch/val repos from Bitbucket if not present locally.
    Required for CI/CD environments where repos aren't pre-cloned.

.PARAMETER Ref
    Git ref (branch/tag) to clone when using -AutoFetch.
    Default: master

.PARAMETER SkipToken
    Skip Step 0 (token refresh). Use when the LLM token is already valid.
    Recommended for local runs where token was recently refreshed.

.PARAMETER DryRun
    Print commands without executing them. Useful for verifying the execution plan.

.PARAMETER Verbose
    Enable DEBUG-level logging in the pipeline output.

.PARAMETER StopOnError
    Stop processing remaining modules when any module fails.
    Default behavior: log the failure and continue to the next module.

.PARAMETER DeleteTemp
    Delete temp/{MODULE}/ after archiving artefacts. Default: keep for reference.

.EXAMPLE
    # Full production run for all modules (token already refreshed)
    .\run_main_pipeline_all_modules.ps1 -Profile mcal -SkipToken -AutoFetch

.EXAMPLE
    # Only EA + source code + SFR + Qdrant steps (skip Jama/requirements)
    .\run_main_pipeline_all_modules.ps1 -Only "4,5,6,7,8" -SkipToken -AutoFetch

.EXAMPLE
    # Dry run to see what would be executed
    .\run_main_pipeline_all_modules.ps1 -DryRun -Profile mcal

.EXAMPLE
    # Run only 3 specific modules with force rebuild
    .\run_main_pipeline_all_modules.ps1 -Modules "ADC,DMA,SPI" -Force -SkipToken

.EXAMPLE
    # CI/CD fresh build: auto-fetch repos, force all, clean slate
    .\run_main_pipeline_all_modules.ps1 -Profile mcal -AutoFetch -Force -Clear -Project A3G

.EXAMPLE
    # Re-run only source code (step 6) and Qdrant (step 8) for all modules
    .\run_main_pipeline_all_modules.ps1 -Only "6,8" -Force -SkipToken
#>

param(
    [ValidateSet("mcal", "test", "local", "illd")]
    [string]$Profile = "mcal",

    [string]$Project = "A3G",

    [string]$Only = "",

    [int]$StartFrom = 0,

    [string]$QeaxPath = "C:\Users\NairSurajRet\Downloads\2.20.0_tc4xx_sw_mcal\2.20.0_tc4xx_sw_mcal.qeax",

    [string]$Modules = "",

    [string]$Ref = "master",

    [switch]$Force,
    [switch]$ForceJama,
    [switch]$Clear,
    [switch]$AutoFetch,
    [switch]$SkipToken,
    [switch]$DryRun,
    [switch]$Verbose,
    [switch]$StopOnError,
    [switch]$DeleteTemp
)

$ErrorActionPreference = "Stop"

# ─── Module List ─────────────────────────────────────────────────────────────
# All 30 A3G MCAL modules in dependency-safe order:
#   - SFR infra is not a module; SFR headers are auto-discovered per module
#   - ETH has two sub-modules (ETH_17_GETH, ETH_17_LETH) handled by run_pipeline.py
$ALL_MODULES = @(
    "ADC",
    "BMC",
    "CAN_17_MCMCAN",
    "CDSP",
    "DIO",
    "DMA",
    "DRE",
    "DSADC",
    "ENCODER",
    "ETH_17_GETH",
    "ETH_17_LETH",
    "FR_17_ERAY",
    "GPT",
    "GTM",
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

# Parse -Modules override
if ($Modules -ne "") {
    $SELECTED_MODULES = @($Modules.Split(",") | ForEach-Object { $_.Trim().ToUpper() })
} else {
    $SELECTED_MODULES = $ALL_MODULES
}

# ─── Resolve Paths ───────────────────────────────────────────────────────────
# $PSScriptRoot = src/HybridRAG/code/ → repo root is 3 levels up
$REPO_ROOT = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$PIPELINE_SCRIPT = Join-Path $REPO_ROOT "src\HybridRAG\code\run_pipeline.py"

if (-not (Test-Path $PIPELINE_SCRIPT)) {
    Write-Host "  ERROR: run_pipeline.py not found at: $PIPELINE_SCRIPT" -ForegroundColor Red
    exit 1
}

# ─── Validate Prerequisites ──────────────────────────────────────────────────
$env:PYTHONIOENCODING = "utf-8"

# Check QEAX file exists (needed for step 4)
$stepsNeedQeax = $true
if ($Only -ne "") {
    $selectedSteps = $Only.Split(",") | ForEach-Object { [int]$_.Trim() }
    if ($selectedSteps -notcontains 4) { $stepsNeedQeax = $false }
} elseif ($StartFrom -gt 4) {
    $stepsNeedQeax = $false
}

if ($stepsNeedQeax -and -not (Test-Path $QeaxPath)) {
    Write-Host "  ERROR: QEAX file not found: $QeaxPath" -ForegroundColor Red
    Write-Host "  Step 4 (EA ingestion) requires this file." -ForegroundColor Red
    Write-Host "  Use -QeaxPath to specify the correct path, or -Only to skip step 4." -ForegroundColor Yellow
    exit 1
}

# ─── Display Execution Plan ──────────────────────────────────────────────────
Write-Host ""
Write-Host ("=" * 72)
Write-Host "  MCAL Main Pipeline — Full Artefact Ingestion"
Write-Host ("=" * 72)
Write-Host "  Profile:    $Profile"
Write-Host "  Project:    $Project"
Write-Host "  Modules:    $($SELECTED_MODULES.Count) modules"
if ($Only -ne "")       { Write-Host "  Steps:      $Only" }
elseif ($StartFrom -gt 0) { Write-Host "  StartFrom:  $StartFrom" }
else                       { Write-Host "  Steps:      ALL (0-8)" }
Write-Host "  QEAX:       $QeaxPath"
Write-Host "  Git ref:    $Ref"
Write-Host "  Flags:      Force=$Force ForceJama=$ForceJama Clear=$Clear AutoFetch=$AutoFetch"
Write-Host "              SkipToken=$SkipToken DryRun=$DryRun Verbose=$Verbose StopOnError=$StopOnError"
Write-Host ("=" * 72)
Write-Host ""
Write-Host "  Module list:"
for ($i = 0; $i -lt $SELECTED_MODULES.Count; $i++) {
    Write-Host "    $($i + 1). $($SELECTED_MODULES[$i])"
}
Write-Host ""

# ─── Build Base Command Arguments ────────────────────────────────────────────
$totalStart = Get-Date
$failed = @()
$completed = 0

foreach ($mod in $SELECTED_MODULES) {
    $completed++
    Write-Host ("─" * 72) -ForegroundColor DarkGray
    Write-Host "  [$completed/$($SELECTED_MODULES.Count)] Processing: $mod" -ForegroundColor Cyan
    Write-Host ("─" * 72) -ForegroundColor DarkGray

    # Build per-module command
    $cmdArgs = @(
        $PIPELINE_SCRIPT,
        "--module", $mod,
        "--profile", $Profile,
        "--project", $Project,
        "--qeax-path", $QeaxPath,
        "--ref", $Ref
    )

    # Step selection
    if ($Only -ne "")          { $cmdArgs += "--only"; $cmdArgs += $Only }
    elseif ($StartFrom -gt 0)  { $cmdArgs += "--start-from"; $cmdArgs += $StartFrom.ToString() }

    # Flags
    if ($SkipToken)   { $cmdArgs += "--skip-token" }
    if ($Force)       { $cmdArgs += "--force" }
    if ($ForceJama)   { $cmdArgs += "--force-jama" }
    if ($Clear)       { $cmdArgs += "--clear" }
    if ($AutoFetch)   { $cmdArgs += "--auto-fetch" }
    if ($DryRun)      { $cmdArgs += "--dry-run" }
    if ($Verbose)     { $cmdArgs += "--verbose" }
    if ($DeleteTemp)  { $cmdArgs += "--delete-temp" }

    $stepStart = Get-Date

    # Execute
    & python @cmdArgs

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  FAILED: $mod (exit code $LASTEXITCODE)" -ForegroundColor Red
        $failed += $mod

        if ($StopOnError) {
            Write-Host "  -StopOnError set. Aborting remaining modules." -ForegroundColor Red
            break
        }
        Write-Host "  Continuing to next module..." -ForegroundColor Yellow
    } else {
        $elapsed = ((Get-Date) - $stepStart).TotalSeconds
        Write-Host "  DONE: $mod ($([math]::Round($elapsed, 1))s)" -ForegroundColor Green
    }
    Write-Host ""
}

# ─── Summary ─────────────────────────────────────────────────────────────────
$totalElapsed = ((Get-Date) - $totalStart).TotalSeconds
$totalMin = [math]::Round($totalElapsed / 60, 1)

Write-Host ""
Write-Host ("=" * 72)
if ($failed.Count -eq 0) {
    Write-Host "  ALL $($SELECTED_MODULES.Count) MODULES COMPLETED SUCCESSFULLY" -ForegroundColor Green
    Write-Host "  Total time: ${totalMin} minutes"
} else {
    Write-Host "  COMPLETED WITH FAILURES" -ForegroundColor Red
    Write-Host "  Failed ($($failed.Count)): $($failed -join ', ')" -ForegroundColor Red
    Write-Host "  Succeeded: $($SELECTED_MODULES.Count - $failed.Count)/$($SELECTED_MODULES.Count)" -ForegroundColor Yellow
    Write-Host "  Total time: ${totalMin} minutes"
    Write-Host ""
    Write-Host "  To retry failed modules:" -ForegroundColor Yellow
    Write-Host "    .\run_main_pipeline_all_modules.ps1 -Modules `"$($failed -join ',')`" <same flags>" -ForegroundColor Yellow
}
Write-Host ("=" * 72)

if ($failed.Count -gt 0) { exit 1 }
