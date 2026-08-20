[CmdletBinding()]
param(
    [string]$OutputPath = ".tmp/harness/harness-manifest.json",
    [string]$RuntimeImageDigest = "",
    [string]$ToolchainDigest = "",
    [switch]$Release
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $result = & git -C $repoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed: $result"
    }
    return @($result)
}

function Get-TextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)

    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $hash = [Security.Cryptography.SHA256]::HashData($bytes)
    return [Convert]::ToHexString($hash).ToLowerInvariant()
}

function Get-TreeDigest {
    param([Parameter(Mandatory = $true)][string[]]$Prefixes)

    $paths = @(
        Invoke-Git -Arguments @("ls-files", "--cached", "--others", "--exclude-standard") |
            Where-Object {
                $candidate = $_.Replace("\", "/")
                foreach ($prefix in $Prefixes) {
                    if ($candidate.StartsWith($prefix, [StringComparison]::Ordinal)) {
                        return $true
                    }
                }
                return $false
            } |
            Sort-Object -Unique
    )
    $entries = foreach ($relativePath in $paths) {
        $absolutePath = Join-Path $repoRoot $relativePath
        if (Test-Path -LiteralPath $absolutePath -PathType Leaf) {
            $fileHash = (Get-FileHash -LiteralPath $absolutePath -Algorithm SHA256).Hash.ToLowerInvariant()
            "$($relativePath.Replace('\', '/'))|$fileHash"
        }
    }
    return Get-TextSha256 -Text (($entries -join "`n") + "`n")
}

$commit = (Invoke-Git -Arguments @("rev-parse", "HEAD") | Select-Object -First 1).Trim()
$statusLines = @(Invoke-Git -Arguments @("status", "--porcelain=v1", "--untracked-files=all"))
$dirty = $statusLines.Count -gt 0

if ($Release -and $dirty) {
    throw "A release harness manifest requires a clean Git worktree."
}
if ($Release -and $RuntimeImageDigest -notmatch '^sha256:[0-9a-fA-F]{64}$') {
    throw "A release harness manifest requires RuntimeImageDigest=sha256:<64 hex>."
}

$sourceTreeDigest = Get-TreeDigest -Prefixes @(
    "backend/",
    "contracts/",
    "src/",
    "frontend/",
    "config/harness/",
    "deploy/k8s/"
)
$bundleDigest = Get-TreeDigest -Prefixes @(
    "src/agents/ratsnestpro/",
    "src/ratsnestpro/",
    "src/evolution/",
    "config/harness/"
)
$contractDigest = Get-TreeDigest -Prefixes @("contracts/")
$policyPath = Join-Path $repoRoot "config/harness/invariants.v1.json"
$policyDigest = (Get-FileHash -LiteralPath $policyPath -Algorithm SHA256).Hash.ToLowerInvariant()

$manifest = [ordered]@{
    schemaVersion = "1.0"
    sourceCommit = $commit
    sourceTreeDigest = $sourceTreeDigest
    dirty = $dirty
    bundleDigest = $bundleDigest
    contractDigest = $contractDigest
    policyDigest = $policyDigest
    runtimeImageDigest = if ($RuntimeImageDigest) { $RuntimeImageDigest } else { $null }
    toolchainDigest = if ($ToolchainDigest) { $ToolchainDigest } else { $null }
}
$canonical = $manifest | ConvertTo-Json -Compress -Depth 8
$manifest["manifestDigest"] = Get-TextSha256 -Text $canonical

$resolvedOutput = if ([IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath
} else {
    Join-Path $repoRoot $OutputPath
}
$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resolvedOutput -Encoding utf8NoBOM

Write-Output $resolvedOutput
