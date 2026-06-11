param(
  [int]$Port = 8086,
  [string]$OnnxFile = "onnx/model.onnx",
  [int]$Forward = 0   # 0 = leave GHOST_B_GLINER_FORWARD unset (pipeline default 8)
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$svcDir = Join-Path $repo "scripts\apple_ml_services"
$py = Join-Path $repo ".venv_onnx\Scripts\python.exe"   # ONNX venv (cu13 ORT nightly)
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("ghost_b_extract_svc_" + $Port + ".log")

# GLiNER -> ONNX Runtime on CUDA; GLiREL stays on torch (cu130). torch is
# imported (via `from gliner import GLiNER`) before onnxruntime, so ORT reuses
# torch's CUDA-13 DLLs — the only arrangement that avoids the cu12/cu13 clash.
$env:GHOST_B_GLINER_ONNX        = "1"
$env:GHOST_B_GLINER_ONNX_REPO   = "E:\Polymath_Training\gliner_onnx_medium_v2.1"
$env:GHOST_B_GLINER_ONNX_FILE   = $OnnxFile
$env:GHOST_B_GLINER_ONNX_DEVICE = "cuda"
$env:GLIREL_CKPT_DIR            = "E:\Polymath_Training\ghost_b_dataset\runs\glirel_ghost_b_v1\best"
$env:GHOST_B_GLINER_BATCH       = "256"
$env:GHOST_B_FACET_BATCH        = "256"
$env:GHOST_B_GLIREL_BATCH       = "512"
$env:PYTHONUNBUFFERED           = "1"
if ($Forward -gt 0) { $env:GHOST_B_GLINER_FORWARD = "$Forward" }

Set-Location $svcDir
$cmdline = "`"$py`" -m uvicorn ghost_b_extract_svc.main:app --host 0.0.0.0 --port $Port >> `"$log`" 2>&1"
cmd.exe /c $cmdline
