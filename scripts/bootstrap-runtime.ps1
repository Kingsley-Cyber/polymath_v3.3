[CmdletBinding()]
param(
    [string]$RuntimeRoot = "",
    [string]$ComposeProfiles = $(if ($env:COMPOSE_PROFILES) { $env:COMPOSE_PROFILES } else { "local-embed,local-rerank,local-parser,mcp" }),
    [switch]$GenerateSecrets,
    [switch]$ForceSecrets,
    [switch]$StageModels,
    [switch]$SkipDockerCheck,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function New-HexSecret {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    } finally {
        $rng.Dispose()
    }
    return (($buffer | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Key,
        [Parameter(Mandatory=$true)][string]$Value
    )

    $escaped = [regex]::Escape($Key)
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            $lines.Add($line) | Out-Null
        }
    }

    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$escaped=") {
            $lines[$i] = "$Key=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) {
        $lines.Add("$Key=$Value") | Out-Null
    }

    if (-not $DryRun) {
        $lines | Set-Content -Encoding UTF8 -LiteralPath $Path
    }
}

function Get-EnvValue {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Key
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $escaped = [regex]::Escape($Key)
    $line = Get-Content -LiteralPath $Path | Where-Object { $_ -match "^$escaped=" } | Select-Object -First 1
    if (-not $line) {
        return ""
    }
    return ($line -replace "^$escaped=", "")
}

function Test-NeedsSecret {
    param([string]$Value)
    return ([string]::IsNullOrWhiteSpace($Value) -or $Value -match "CHANGE_ME")
}

function Copy-SeedFile {
    param(
        [Parameter(Mandatory=$true)][string]$Source,
        [Parameter(Mandatory=$true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Missing required repo file: $Source"
    }
    $parent = Split-Path -Parent $Destination
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
}

function Invoke-HfDownload {
    param(
        [Parameter(Mandatory=$true)][string]$Model,
        [Parameter(Mandatory=$true)][string]$Destination
    )
    $hf = Get-Command hf -ErrorAction SilentlyContinue
    $legacy = Get-Command huggingface-cli -ErrorAction SilentlyContinue
    $cmd = if ($hf) { $hf.Source } elseif ($legacy) { $legacy.Source } else { $null }
    if (-not $cmd) {
        Write-Warning "Hugging Face CLI not found. Install with: pip install -U huggingface_hub"
        return
    }
    if ($DryRun) {
        Write-Host "Would download $Model to $Destination"
        return
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    & $cmd download $Model --local-dir $Destination
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$envFile = Join-Path $repoRoot ".env"
$envExample = Join-Path $repoRoot ".env.example"

if (-not $RuntimeRoot) {
    if ($env:POLYMATH_DOCKER_DATA_ROOT) {
        $RuntimeRoot = $env:POLYMATH_DOCKER_DATA_ROOT
    } elseif (Test-Path -LiteralPath $envFile) {
        $existingRoot = Get-EnvValue -Path $envFile -Key "POLYMATH_DOCKER_DATA_ROOT"
        $RuntimeRoot = if ($existingRoot) { $existingRoot } else { "C:\PolymathRuntime" }
    } else {
        $RuntimeRoot = "C:\PolymathRuntime"
    }
}

$runtimeRootFull = [System.IO.Path]::GetFullPath($RuntimeRoot)
$bindsRoot = Join-Path $runtimeRootFull "binds"
$modelsRoot = Join-Path $runtimeRootFull "models"
$cacheRoot = $runtimeRootFull

Write-Step "Preparing Polymath runtime at $runtimeRootFull"

if (-not (Test-Path -LiteralPath $envFile)) {
    if (-not (Test-Path -LiteralPath $envExample)) {
        throw "Missing .env.example"
    }
    Write-Step "Creating .env from .env.example"
    if (-not $DryRun) {
        Copy-Item -LiteralPath $envExample -Destination $envFile -Force
    }
}

$directories = @(
    $runtimeRootFull,
    (Join-Path $runtimeRootFull "volumes\mongodb"),
    (Join-Path $runtimeRootFull "volumes\qdrant"),
    (Join-Path $runtimeRootFull "volumes\neo4j\data"),
    (Join-Path $runtimeRootFull "volumes\neo4j\plugins"),
    (Join-Path $runtimeRootFull "volumes\neo4j\logs"),
    (Join-Path $runtimeRootFull "volumes\redis"),
    (Join-Path $runtimeRootFull "volumes\hf-cache"),
    (Join-Path $runtimeRootFull "volumes\docling\models"),
    $bindsRoot,
    (Join-Path $bindsRoot "litellm"),
    $modelsRoot
)

foreach ($dir in $directories) {
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
}

Write-Step "Seeding bind-mounted config files"
Copy-SeedFile `
    -Source (Join-Path $repoRoot "litellm\config.yaml") `
    -Destination (Join-Path $bindsRoot "litellm\config.yaml")
Copy-SeedFile `
    -Source (Join-Path $repoRoot "modal_embedder.py") `
    -Destination (Join-Path $bindsRoot "modal_embedder.py")

Set-EnvValue -Path $envFile -Key "POLYMATH_DOCKER_DATA_ROOT" -Value ($runtimeRootFull -replace "\\", "/")
Set-EnvValue -Path $envFile -Key "POLYMATH_RUNTIME_BINDS_ROOT" -Value ($bindsRoot -replace "\\", "/")
Set-EnvValue -Path $envFile -Key "POLYMATH_CACHE_ROOT" -Value ($cacheRoot -replace "\\", "/")
Set-EnvValue -Path $envFile -Key "POLYMATH_MODELS_ROOT" -Value ($modelsRoot -replace "\\", "/")
Set-EnvValue -Path $envFile -Key "COMPOSE_PROFILES" -Value $ComposeProfiles
Set-EnvValue -Path $envFile -Key "LOCAL_EMBEDDER_ENABLED" -Value "true"
Set-EnvValue -Path $envFile -Key "LOCAL_RERANKER_ENABLED" -Value "true"

if ($GenerateSecrets) {
    Write-Step "Generating missing secrets"
    $mongoPassword = Get-EnvValue -Path $envFile -Key "MONGO_PASSWORD"
    if ($ForceSecrets -or (Test-NeedsSecret $mongoPassword)) {
        $mongoPassword = New-HexSecret 24
        Set-EnvValue -Path $envFile -Key "MONGO_PASSWORD" -Value $mongoPassword
    }
    Set-EnvValue -Path $envFile -Key "MONGODB_URI" -Value "mongodb://polymath:$mongoPassword@mongodb:27017/polymath?authSource=admin"

    $secretKeys = @{
        "NEO4J_PASSWORD" = 24
        "AUTH_SECRET_KEY" = 48
        "DEFAULT_ADMIN_PASSWORD" = 18
        "LITELLM_MASTER_KEY" = 32
        "MCP_API_KEY" = 32
    }
    foreach ($entry in $secretKeys.GetEnumerator()) {
        $current = Get-EnvValue -Path $envFile -Key $entry.Key
        if ($ForceSecrets -or (Test-NeedsSecret $current)) {
            Set-EnvValue -Path $envFile -Key $entry.Key -Value (New-HexSecret $entry.Value)
        }
    }
}

if (-not $SkipDockerCheck) {
    Write-Step "Checking Docker"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker was not found on PATH. Install Docker Desktop or Docker Engine first."
    }
    if (-not $DryRun) {
        docker compose config --quiet
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose config failed. Check .env values and bind-mounted files."
        }
    }
}

if ($StageModels) {
    Write-Step "Downloading local embedding and reranker models"
    Invoke-HfDownload -Model "Qwen/Qwen3-Embedding-0.6B" -Destination (Join-Path $modelsRoot "Qwen3-Embedding-0.6B")
    Invoke-HfDownload -Model "cross-encoder/ms-marco-MiniLM-L6-v2" -Destination (Join-Path $modelsRoot "ms-marco-MiniLM-L6-v2")
} else {
    Write-Host "Skipping model downloads. Re-run with -StageModels when ready."
}

Write-Host ""
Write-Host "Polymath runtime bootstrap complete."
Write-Host "Next:"
Write-Host "  docker compose up -d --build"
Write-Host "  .\scripts\check-install.ps1"
Write-Host "  open http://localhost:3000"
