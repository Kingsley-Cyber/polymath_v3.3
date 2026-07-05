param(
    # Port per instance: Settings->Ingestion defines GPU box rows on :8084
    # and :8085 (two sidecar instances on one box = 2-way slice parallelism).
    # Start each:  .\run_sidecar_windows.ps1            (defaults to 8084)
    #              .\run_sidecar_windows.ps1 -Port 8085
    [int]$Port = 8084
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$svcDir = Join-Path $repo "scripts\apple_ml_services"
$py = Join-Path $repo ".venv_sidecar\Scripts\python.exe"
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "ghost_b_extract_svc_$Port.log"

$env:GHOST_B_GLINER_BATCH = "256"
$env:GHOST_B_GLIREL_BATCH = "512"
$env:GHOST_B_FACET_BATCH  = "256"
$env:GLIREL_CKPT_DIR      = "E:\Polymath_Training\ghost_b_dataset\runs\glirel_ghost_b_v1\best"
$env:PYTHONUNBUFFERED     = "1"

Set-Location $svcDir
# Use cmd.exe for redirection. PowerShell's `*>>` wraps native-process stderr
# (uvicorn writes its startup banner there) as a NativeCommandError, which
# aborts this script before uvicorn binds the port. cmd's `>> ... 2>&1` is
# byte-stream redirection with no such interpretation.
$cmdline = "`"$py`" -m uvicorn ghost_b_extract_svc.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
cmd.exe /c $cmdline
