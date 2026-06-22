[CmdletBinding()]
param(
    [string]$RuntimeRoot = $(if ($env:POLYMATH_DOCKER_DATA_ROOT) { $env:POLYMATH_DOCKER_DATA_ROOT } else { "" }),
    [switch]$SkipComposeConfig,
    [switch]$SkipRuntimeContracts,
    [switch]$CheckRunning
)

$ErrorActionPreference = "Stop"
$failures = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()

function Add-Failure {
    param([string]$Message)
    $failures.Add($Message) | Out-Null
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Add-Warning {
    param([string]$Message)
    $warnings.Add($Message) | Out-Null
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Add-Ok {
    param([string]$Message)
    Write-Host "[ OK ] $Message" -ForegroundColor Green
}

function Get-EnvMap {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $map
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*#" -or $line -notmatch "=") {
            continue
        }
        $key, $value = $line -split "=", 2
        $map[$key.Trim()] = $value
    }
    return $map
}

function Test-Http {
    param(
        [string]$Name,
        [string]$Url,
        [bool]$Required = $true
    )
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
            Add-Ok "$Name reachable ($Url)"
        } elseif ($Required) {
            Add-Failure "$Name returned HTTP $($response.StatusCode): $Url"
        } else {
            Add-Warning "$Name returned HTTP $($response.StatusCode): $Url"
        }
    } catch {
        if ($Required) {
            Add-Failure "$Name unreachable: $Url ($($_.Exception.Message))"
        } else {
            Add-Warning "$Name unreachable: $Url ($($_.Exception.Message))"
        }
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$envFile = Join-Path $repoRoot ".env"

Write-Host "Polymath install check"
Write-Host "Repo: $repoRoot"

if (-not $SkipRuntimeContracts) {
    $contractScript = Join-Path $repoRoot "scripts\verify_runtime_contracts.py"
    $secretScanScript = Join-Path $repoRoot "scripts\scan_tracked_secrets.py"
    $python = Get-Command python3 -ErrorAction SilentlyContinue
    if (-not $python) {
        $python = Get-Command python -ErrorAction SilentlyContinue
    }

    if ($python) {
        Push-Location $repoRoot
        try {
            & $python.Source $contractScript
            if ($LASTEXITCODE -eq 0) {
                Add-Ok "Runtime setup/worker/trigger contracts are intact"
            } else {
                Add-Failure "Runtime setup/worker/trigger contract check failed"
            }
        } catch {
            Add-Failure "Runtime setup/worker/trigger contract check failed"
        } finally {
            Pop-Location
        }
        Push-Location $repoRoot
        try {
            & $python.Source $secretScanScript --quiet
            if ($LASTEXITCODE -eq 0) {
                Add-Ok "No tracked API keys or secrets detected"
            } else {
                Add-Failure "Tracked secret scan failed"
            }
        } catch {
            Add-Failure "Tracked secret scan failed"
        } finally {
            Pop-Location
        }
    } else {
        Add-Warning "Python not found; skipping runtime contract and tracked secret checks"
    }
}

if (-not (Test-Path -LiteralPath $envFile)) {
    Add-Failure ".env is missing. Run .\scripts\bootstrap-runtime.ps1 -GenerateSecrets"
    $envMap = @{}
} else {
    Add-Ok ".env exists"
    $envMap = Get-EnvMap -Path $envFile
}

if (-not $RuntimeRoot) {
    $RuntimeRoot = $envMap["POLYMATH_DOCKER_DATA_ROOT"]
}
if (-not $RuntimeRoot) {
    $RuntimeRoot = "C:\PolymathRuntime"
}

$requiredSecrets = @(
    "MONGO_PASSWORD",
    "NEO4J_PASSWORD",
    "AUTH_SECRET_KEY",
    "DEFAULT_ADMIN_PASSWORD",
    "LITELLM_MASTER_KEY",
    "MCP_API_KEY"
)

foreach ($key in $requiredSecrets) {
    $value = if ($envMap.ContainsKey($key)) { $envMap[$key] } else { "" }
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match "CHANGE_ME") {
        Add-Failure "$key is missing or still has CHANGE_ME"
    } else {
        Add-Ok "$key is set"
    }
}

$bindsRoot = if ($envMap.ContainsKey("POLYMATH_RUNTIME_BINDS_ROOT")) {
    $envMap["POLYMATH_RUNTIME_BINDS_ROOT"]
} else {
    Join-Path $RuntimeRoot "binds"
}

$checks = @(
    (Join-Path $bindsRoot "litellm\config.yaml"),
    (Join-Path $bindsRoot "modal_embedder.py"),
    (Join-Path $RuntimeRoot "volumes\mongodb"),
    (Join-Path $RuntimeRoot "volumes\qdrant"),
    (Join-Path $RuntimeRoot "volumes\neo4j\data")
)

foreach ($path in $checks) {
    if (Test-Path -LiteralPath $path) {
        Add-Ok "Found $path"
    } else {
        Add-Failure "Missing $path"
    }
}

$modelRoot = if ($envMap.ContainsKey("POLYMATH_MODELS_ROOT")) {
    $envMap["POLYMATH_MODELS_ROOT"]
} else {
    Join-Path $RuntimeRoot "models"
}

foreach ($model in @("Qwen3-Embedding-0.6B", "Qwen3-Reranker-0.6B-Q8_0-GGUF")) {
    $path = Join-Path $modelRoot $model
    if (Test-Path -LiteralPath $path) {
        Add-Ok "Found model directory $path"
    } else {
        Add-Warning "Model directory missing: $path. Run bootstrap with -StageModels or use cloud embeddings."
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Add-Failure "Docker is not on PATH"
} else {
    Add-Ok "Docker is on PATH"
    $overrideFile = Join-Path $repoRoot "docker-compose.override.yml"
    if (Test-Path -LiteralPath $overrideFile) {
        Add-Warning "Local docker-compose.override.yml detected; docker compose will auto-merge machine-specific overrides."
    }
    if (-not $SkipComposeConfig) {
        Push-Location $repoRoot
        try {
            docker compose config --quiet
            if ($LASTEXITCODE -eq 0) {
                Add-Ok "docker compose config is valid"
            } else {
                Add-Failure "docker compose config failed"
            }
        } catch {
            Add-Failure "docker compose config failed"
        } finally {
            Pop-Location
        }
    }
}

if ($CheckRunning) {
    Test-Http -Name "Frontend" -Url "http://localhost:3000" -Required $true
    Test-Http -Name "Backend health" -Url "http://localhost:8000/api/health" -Required $true
    Test-Http -Name "MCP health" -Url "http://localhost:8765/health" -Required $false
    Test-Http -Name "Qdrant" -Url "http://localhost:6333/healthz" -Required $false
    Test-Http -Name "Neo4j browser" -Url "http://localhost:7474" -Required $false
}

Write-Host ""
Write-Host "Summary: $($failures.Count) failure(s), $($warnings.Count) warning(s)"
if ($failures.Count -gt 0) {
    exit 1
}
exit 0
