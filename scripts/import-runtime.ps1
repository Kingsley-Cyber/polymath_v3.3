[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [string]$RuntimeRoot = $(if ($env:POLYMATH_DOCKER_DATA_ROOT) { $env:POLYMATH_DOCKER_DATA_ROOT } else { "C:\PolymathRuntime" }),
    [switch]$IncludeEnv,
    [switch]$AllowRunning,
    [switch]$Merge,
    [switch]$OverwriteEnv
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
        throw "Polymath containers are running. Run 'docker compose down' first, or pass -AllowRunning if you accept importing over live services."
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$tempExtract = $null
$resolvedSource = (Resolve-Path -LiteralPath $Source).Path
if (Test-Path -LiteralPath $resolvedSource -PathType Leaf) {
    $extension = [System.IO.Path]::GetExtension("$resolvedSource").ToLowerInvariant()
    if ($extension -ne ".zip") {
        throw "Compressed imports on Windows expect a .zip file: $resolvedSource"
    }
    $tempExtract = Join-Path ([System.IO.Path]::GetTempPath()) ("polymath-runtime-import-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Force -Path $tempExtract | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory("$resolvedSource", $tempExtract)
    $sourcePath = Resolve-Path -LiteralPath $tempExtract
} else {
    $sourcePath = $resolvedSource
}
$sourceRuntime = Join-Path $sourcePath "runtime"
if (-not (Test-Path -LiteralPath $sourceRuntime)) {
    throw "Runtime export is missing the 'runtime' directory: $sourceRuntime"
}

Assert-StackStopped

if ((Test-Path -LiteralPath $RuntimeRoot) -and -not $Merge) {
    $existing = @(Get-ChildItem -Force -LiteralPath $RuntimeRoot -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        throw "Runtime root already has files: $RuntimeRoot. Pass -Merge to import into it."
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
Get-ChildItem -Force -LiteralPath $sourceRuntime | Copy-Item -Destination $RuntimeRoot -Recurse -Force

if ($IncludeEnv) {
    $sourceEnv = Join-Path $sourcePath "repo\.env"
    if (-not (Test-Path -LiteralPath $sourceEnv)) {
        Write-Warning "Requested -IncludeEnv, but export does not contain repo/.env"
    } else {
        $targetEnv = Join-Path $repoRoot ".env"
        if ((Test-Path -LiteralPath $targetEnv) -and -not $OverwriteEnv) {
            throw ".env already exists at $targetEnv. Pass -OverwriteEnv to replace it."
        }
        Copy-Item -LiteralPath $sourceEnv -Destination $targetEnv -Force
    }
}

Write-Host "Imported Polymath runtime core into: $RuntimeRoot"

if ($tempExtract -and (Test-Path -LiteralPath $tempExtract)) {
    Remove-Item -LiteralPath $tempExtract -Recurse -Force
}
