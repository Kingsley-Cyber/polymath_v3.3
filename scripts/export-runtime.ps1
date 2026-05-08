[CmdletBinding()]
param(
    [string]$RuntimeRoot = $(if ($env:POLYMATH_DOCKER_DATA_ROOT) { $env:POLYMATH_DOCKER_DATA_ROOT } else { "C:\PolymathRuntime" }),
    [string]$Destination = $(Join-Path (Get-Location) ("polymath-runtime-core-" + (Get-Date -Format "yyyyMMdd-HHmmss"))),
    [switch]$IncludeEnv,
    [switch]$IncludeModels,
    [switch]$Archive,
    [string]$ArchivePath,
    [switch]$AllowRunning,
    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"

function Assert-StackStopped {
    if ($AllowRunning) {
        return
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        return
    }
    $running = @(
        docker ps --filter "label=com.docker.compose.project=polymath_v33" --format "{{.Names}}" 2>$null
        docker ps --filter "name=polymath-mcp" --format "{{.Names}}" 2>$null
    ) | Sort-Object -Unique
    if ($running.Count -gt 0) {
        throw "Polymath containers are running. Run 'docker compose down' first, or pass -AllowRunning if you accept a crash-consistency backup."
    }
}

function Copy-RelativeItem {
    param(
        [Parameter(Mandatory=$true)][string]$Root,
        [Parameter(Mandatory=$true)][string]$Relative,
        [Parameter(Mandatory=$true)][string]$TargetRoot,
        [System.Collections.Generic.List[string]]$Copied
    )

    $source = Join-Path $Root $Relative
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Warning "Skipping missing runtime item: $Relative"
        return
    }

    $target = Join-Path $TargetRoot $Relative
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null

    $item = Get-Item -LiteralPath $source
    if ($item.PSIsContainer) {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Get-ChildItem -Force -LiteralPath $source | Copy-Item -Destination $target -Recurse -Force
    } else {
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
    $Copied.Add($Relative) | Out-Null
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$runtimePath = Resolve-Path -LiteralPath $RuntimeRoot

if ((Test-Path -LiteralPath $Destination) -and -not $Overwrite) {
    $existing = @(Get-ChildItem -Force -LiteralPath $Destination -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        throw "Destination already exists and is not empty: $Destination. Pass -Overwrite or choose a new path."
    }
}

Assert-StackStopped

$runtimeTarget = Join-Path $Destination "runtime"
New-Item -ItemType Directory -Force -Path $runtimeTarget | Out-Null

$copied = [System.Collections.Generic.List[string]]::new()
$coreItems = @(
    "volumes/mongodb",
    "volumes/qdrant",
    "volumes/neo4j",
    "volumes/redis",
    "volumes/n8n",
    "binds/litellm",
    "binds/modal_embedder.py"
)

foreach ($item in $coreItems) {
    Copy-RelativeItem -Root $runtimePath -Relative $item -TargetRoot $runtimeTarget -Copied $copied
}

if ($IncludeModels) {
    Copy-RelativeItem -Root $runtimePath -Relative "models" -TargetRoot $runtimeTarget -Copied $copied
}

if ($IncludeEnv) {
    $envFile = Join-Path $repoRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        $repoTarget = Join-Path $Destination "repo"
        New-Item -ItemType Directory -Force -Path $repoTarget | Out-Null
        Copy-Item -LiteralPath $envFile -Destination (Join-Path $repoTarget ".env") -Force
    } else {
        Write-Warning "Requested -IncludeEnv, but .env was not found at $envFile"
    }
}

$manifest = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    repo_root = "$repoRoot"
    runtime_root = "$runtimePath"
    include_env = [bool]$IncludeEnv
    include_models = [bool]$IncludeModels
    copied_items = $copied
    note = "Stop containers before export/import. Mongo, Qdrant, and Neo4j are the ingestion-critical stores."
}

$manifest | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -Path (Join-Path $Destination "manifest.json")

if ($Archive -or $ArchivePath) {
    if (-not $ArchivePath) {
        $ArchivePath = "$Destination.zip"
    }
    $archiveFullPath = [System.IO.Path]::GetFullPath($ArchivePath)
    $archiveParent = Split-Path -Parent $archiveFullPath
    if ($archiveParent) {
        New-Item -ItemType Directory -Force -Path $archiveParent | Out-Null
    }
    if ((Test-Path -LiteralPath $archiveFullPath) -and -not $Overwrite) {
        throw "Archive already exists: $archiveFullPath. Pass -Overwrite or choose a new -ArchivePath."
    }
    if (Test-Path -LiteralPath $archiveFullPath) {
        Remove-Item -LiteralPath $archiveFullPath -Force
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        [System.IO.Path]::GetFullPath($Destination),
        $archiveFullPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    Write-Host "Created portable archive: $archiveFullPath"
}

Write-Host "Exported Polymath runtime core to: $Destination"
