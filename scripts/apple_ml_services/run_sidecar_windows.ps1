$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$svcDir = Join-Path $repo "scripts\apple_ml_services"
$py = Join-Path $repo ".venv_sidecar\Scripts\python.exe"
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "ghost_b_extract_svc.log"

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
$cmdline = "`"$py`" -m uvicorn ghost_b_extract_svc.main:app --host 0.0.0.0 --port 8084 >> `"$log`" 2>&1"
cmd.exe /c $cmdline
