param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RemoveHistoricalRuns,
    [string[]]$PreserveRuns = @()
)

$root = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ProjectRoot).Path).TrimEnd("\")
$targets = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)

function Add-Target([string]$Candidate) {
    if (-not (Test-Path -LiteralPath $Candidate)) {
        return
    }
    $item = Get-Item -LiteralPath $Candidate -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to clean a reparse point: $($item.FullName)"
    }
    $resolved = [IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $Candidate).Path
    )
    if (-not $resolved.StartsWith(
        $root + "\",
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Cleanup target escaped project root: $resolved"
    }
    [void]$targets.Add($resolved)
}

Get-ChildItem -LiteralPath $root -Directory -Force |
    Where-Object {
        $_.Name -in @(".pytest_cache", ".ruff_cache") -or
        $_.Name -like ".pytest-tmp-*"
    } |
    ForEach-Object { Add-Target $_.FullName }

# Reproducible build outputs are not source and must not accumulate in a
# release workspace. Keep dependency environments and the shared BuildKit
# cache because they are intentionally reusable.
foreach ($relative in @(
    "backend\bin",
    "backend\target",
    "frontend\.next",
    "frontend\tsconfig.tsbuildinfo",
    ".ruff-cache"
)) {
    Add-Target (Join-Path $root $relative)
}

foreach ($base in @("src", "tests", "scripts")) {
    $path = Join-Path $root $base
    if (Test-Path -LiteralPath $path) {
        Get-ChildItem -LiteralPath $path -Directory -Recurse -Force |
            Where-Object { $_.Name -eq "__pycache__" } |
            ForEach-Object { Add-Target $_.FullName }
    }
}

$edaKernel = Join-Path $root "src\ratsnestpro"
if (Test-Path -LiteralPath $edaKernel) {
    Get-ChildItem -LiteralPath $edaKernel -Directory -Force |
        Where-Object {
            $_.Name -in @(".pytest_cache", ".ruff_cache", "Temp") -or
            $_.Name -like ".pytest-tmp-*" -or
            $_.Name -like ".debug-*" -or
            $_.Name -like ".tmp-lock-*" -or
            $_.Name -like ".tv9*" -or
            $_.Name -like ".tp1*" -or
            $_.Name -like ".tcc*" -or
            $_.Name -like "tmp*"
        } |
        ForEach-Object { Add-Target $_.FullName }
}

$dataRoot = Join-Path $root "data"
if (Test-Path -LiteralPath $dataRoot) {
    Get-ChildItem -LiteralPath $dataRoot -Directory -Force |
        Where-Object {
            $_.Name -like "pytest-*" -or
            $_.Name -like "tmp-pytest-*" -or
            $_.Name -like "pycache*" -or
            $_.Name -eq "checkpoint-backups"
        } |
        ForEach-Object { Add-Target $_.FullName }
}

Add-Target (Join-Path $root "data\ratsnestpro\e2e-tests")

$ratsnestData = Join-Path $root "data\ratsnestpro"
if (Test-Path -LiteralPath $ratsnestData) {
    Get-ChildItem -LiteralPath $ratsnestData -File -Force |
        Where-Object {
            $_.Name -like "checkpoints-pre-*" -or
            $_.Name -like "codex-*"
        } |
        ForEach-Object { Add-Target $_.FullName }
}

$runsRoot = Join-Path $root "data\ratsnestpro\runs"
if ($RemoveHistoricalRuns -and (Test-Path -LiteralPath $runsRoot)) {
    Get-ChildItem -LiteralPath $runsRoot -Directory -Force |
        Where-Object {
            $_.Name -ne ".locks" -and $_.Name -notin $PreserveRuns
        } |
        ForEach-Object { Add-Target $_.FullName }
}

$reviewsRoot = Join-Path $root "data\ratsnestpro\reviews"
if ($RemoveHistoricalRuns -and (Test-Path -LiteralPath $reviewsRoot)) {
    Get-ChildItem -LiteralPath $reviewsRoot -File -Force |
        Where-Object {
            $_.BaseName -notmatch "-review$" -or
            $_.BaseName.Substring(0, $_.BaseName.Length - 7) -notin $PreserveRuns
        } |
        ForEach-Object { Add-Target $_.FullName }
}

$bytes = 0L
foreach ($target in $targets) {
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $bytes += (Get-Item -LiteralPath $target -Force).Length
        continue
    }
    $sum = (
        Get-ChildItem -LiteralPath $target -File -Recurse -Force |
            Measure-Object Length -Sum
    ).Sum
    if ($null -ne $sum) {
        $bytes += [long]$sum
    }
}

foreach ($target in $targets | Sort-Object Length -Descending) {
    Remove-Item -LiteralPath $target -Recurse -Force
}

# Source-control does not preserve empty directories. Remove any that remain
# after cache cleanup so a copied/unpacked workspace has the same structure as
# a clean clone. Work bottom-up and retain the same project-root boundary used
# for recursive targets above.
$removedEmptyDirectories = 0
foreach ($base in @("src", "tests", "scripts", "docs")) {
    $path = Join-Path $root $base
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        continue
    }
    $directories = Get-ChildItem -LiteralPath $path -Directory -Recurse -Force |
        Sort-Object { $_.FullName.Length } -Descending
    foreach ($directory in $directories) {
        if (-not (Test-Path -LiteralPath $directory.FullName -PathType Container)) {
            continue
        }
        if (Get-ChildItem -LiteralPath $directory.FullName -Force | Select-Object -First 1) {
            continue
        }
        $resolved = [IO.Path]::GetFullPath(
            (Resolve-Path -LiteralPath $directory.FullName).Path
        )
        if (-not $resolved.StartsWith(
            $root + "\",
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Empty-directory cleanup target escaped project root: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Force
        $removedEmptyDirectories++
    }
}

[pscustomobject]@{
    RemovedTargets = $targets.Count
    RemovedEmptyDirectories = $removedEmptyDirectories
    ReclaimedMB = [math]::Round($bytes / 1MB, 2)
    HistoricalRunsRemoved = [bool]$RemoveHistoricalRuns
    PreservedEhe = Test-Path -LiteralPath (Join-Path $root "data\ratsnestpro\ehe")
}
