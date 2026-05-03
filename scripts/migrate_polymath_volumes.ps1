# Migrate Polymath docker volumes from C:\PolymathRuntime to E:\PolymathRuntime.
#
# WHY: docker-compose.yml binds all volumes under POLYMATH_DOCKER_DATA_ROOT.
# When unset, default is C:/PolymathRuntime. On a machine where C: is full
# this starves mongo / neo4j / qdrant / docling / ingest-spool. Moving to E:
# requires (a) container downtime so file handles are released, (b) a one-time
# data copy, (c) re-up with the new env value already set in .env.
#
# This script does NOT delete the source volumes — it copies, verifies, and
# leaves the originals in place. After you confirm the new ones boot cleanly,
# run with -DeleteSource to reclaim disk on C:.
#
# Usage (from repo root, in an elevated PowerShell):
#   .\scripts\migrate_polymath_volumes.ps1
#   .\scripts\migrate_polymath_volumes.ps1 -DryRun
#   .\scripts\migrate_polymath_volumes.ps1 -DeleteSource
#
# Safety:
#   * Refuses to run if any polymath_v33-* container is running.
#   * Uses robocopy /MIR with retry caps; logs every file count.
#   * Per-volume checksum (file count + total bytes) compared after copy.
#   * Source untouched unless -DeleteSource is passed.

[CmdletBinding()]
param(
    [string]$Source = "C:\PolymathRuntime",
    [string]$Target = "E:\PolymathRuntime",
    [switch]$DryRun,
    [switch]$DeleteSource
)

$ErrorActionPreference = "Stop"

function Write-Step { param($Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Ok { param($Msg) Write-Host "    [ok] $Msg" -ForegroundColor Green }
function Write-Warn { param($Msg) Write-Host "    [warn] $Msg" -ForegroundColor Yellow }
function Fail { param($Msg) Write-Host "    [fail] $Msg" -ForegroundColor Red; exit 1 }

# --- Pre-flight ---
Write-Step "Pre-flight checks"

if (-not (Test-Path $Source)) { Fail "Source missing: $Source" }
if (-not (Test-Path "E:\")) { Fail "E: drive not present" }

# Refuse if any polymath container is running — file handles must be released.
$running = docker ps --filter "name=polymath_v33-" --format "{{.Names}}" 2>$null
if ($running) {
    Write-Host ""
    Write-Host "These polymath containers are running:" -ForegroundColor Yellow
    $running | ForEach-Object { Write-Host "  - $_" }
    Fail "Run ``docker compose down`` first, then re-run this script."
}
Write-Ok "No polymath containers running."

# Disk space sanity
$srcSize = (Get-ChildItem -LiteralPath $Source -Recurse -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
$srcGB = [math]::Round($srcSize / 1GB, 2)
$tgtFree = (Get-PSDrive -Name (Split-Path $Target -Qualifier).TrimEnd(':')).Free
$tgtFreeGB = [math]::Round($tgtFree / 1GB, 2)
Write-Ok "Source size: $srcGB GB"
Write-Ok "Target free: $tgtFreeGB GB on $(Split-Path $Target -Qualifier)"
if ($tgtFree -lt $srcSize * 1.1) {
    Fail "Target drive needs at least $([math]::Round($srcGB * 1.1, 2)) GB free; has $tgtFreeGB GB."
}

if ($DryRun) {
    Write-Step "DRY RUN — no files will be copied. Listing what would move:"
    Get-ChildItem -LiteralPath "$Source\volumes" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "  $($_.Name)" }
    exit 0
}

# --- Migration ---
New-Item -ItemType Directory -Path $Target -Force | Out-Null
New-Item -ItemType Directory -Path "$Target\volumes" -Force | Out-Null

$volumes = Get-ChildItem -LiteralPath "$Source\volumes" -Directory -ErrorAction SilentlyContinue
if (-not $volumes) {
    Write-Warn "No volumes under $Source\volumes — nothing to migrate."
    exit 0
}

$summary = @()
foreach ($vol in $volumes) {
    $name = $vol.Name
    $src = $vol.FullName
    $tgt = Join-Path "$Target\volumes" $name
    Write-Step "Migrating $name"

    # /MIR mirrors source to target (deletes target-only files first if any)
    # /R:3 /W:5 — retry up to 3 times, 5s wait
    # /NP — no per-file progress (faster on slow disks)
    # /NFL /NDL — quiet on file/dir lists; we only want totals at the end
    # Note: robocopy exits 0–7 for "OK", 8+ is failure.
    robocopy $src $tgt /MIR /R:3 /W:5 /NP /NFL /NDL | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -ge 8) { Fail "robocopy failed (exit $rc) for $name" }

    $srcCount = (Get-ChildItem -LiteralPath $src -Recurse -File -ErrorAction SilentlyContinue).Count
    $tgtCount = (Get-ChildItem -LiteralPath $tgt -Recurse -File -ErrorAction SilentlyContinue).Count
    $srcBytes = (Get-ChildItem -LiteralPath $src -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object -Property Length -Sum).Sum
    $tgtBytes = (Get-ChildItem -LiteralPath $tgt -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object -Property Length -Sum).Sum

    if ($srcCount -ne $tgtCount -or $srcBytes -ne $tgtBytes) {
        Fail "$name verification failed: src=$srcCount/$srcBytes  tgt=$tgtCount/$tgtBytes"
    }
    $sizeGB = [math]::Round($srcBytes / 1GB, 2)
    Write-Ok "$name OK ($srcCount files, $sizeGB GB)"
    $summary += [pscustomobject]@{ Name = $name; Files = $srcCount; SizeGB = $sizeGB }
}

Write-Step "Summary"
$summary | Format-Table -AutoSize

# --- Post-flight ---
Write-Step "Next steps"
Write-Host "  1. Verify .env has POLYMATH_DOCKER_DATA_ROOT=E:/PolymathRuntime"
Write-Host "  2. docker compose up -d"
Write-Host "  3. Run a small smoke ingest to confirm mongo / qdrant / neo4j picked up the new paths"
Write-Host "  4. Once happy, re-run this script with -DeleteSource to reclaim ~$srcGB GB on C:"

if ($DeleteSource) {
    Write-Step "Removing source after successful migration (-DeleteSource set)"
    Remove-Item -LiteralPath "$Source\volumes" -Recurse -Force
    Write-Ok "Source volumes removed. C: drive freed."
}
