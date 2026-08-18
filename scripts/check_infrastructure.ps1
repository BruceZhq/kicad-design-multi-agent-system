[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

function Assert-LocalWorkflowReferences {
    $workflowRoot = Join-Path $projectRoot ".github/workflows"
    foreach ($workflow in Get-ChildItem -LiteralPath $workflowRoot -File -Include *.yml, *.yaml) {
        $contents = Get-Content -Raw -LiteralPath $workflow.FullName
        foreach ($match in [regex]::Matches(
                $contents,
                'uses:\s*(\./\.github/workflows/[^\s#]+)')) {
            $relativePath = $match.Groups[1].Value.Substring(2).Replace("/", [IO.Path]::DirectorySeparatorChar)
            $target = Join-Path $projectRoot $relativePath
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
                throw "Workflow $($workflow.Name) references a missing local workflow: $target"
            }
        }
    }
}

function Assert-PowerShellSyntax {
    $scripts = @(
        "scripts/deploy_harness_canary.ps1",
        "scripts/promote_harness_canary.ps1",
        "scripts/rollback_harness.ps1"
    )
    foreach ($relativePath in $scripts) {
        $path = Join-Path $projectRoot $relativePath
        $tokens = $null
        $errors = $null
        [void] [System.Management.Automation.Language.Parser]::ParseFile(
            $path,
            [ref] $tokens,
            [ref] $errors
        )
        if ($errors.Count -gt 0) {
            throw "PowerShell syntax error in ${relativePath}: $($errors -join '; ')"
        }
    }
}

function Assert-EvolutionWorkerContract {
    $composePath = Join-Path $projectRoot "compose.yaml"
    $dockerfilePath = Join-Path $projectRoot "docker/Dockerfile.evolution"
    $compose = Get-Content -Raw -LiteralPath $composePath
    $dockerfile = Get-Content -Raw -LiteralPath $dockerfilePath
    foreach ($evidence in @(
            "evolution_worker:",
            "profiles: [evolution]",
            "RATSNEST_EVOLUTION_REPOSITORY_ROOT=/repository",
            "RATSNEST_EVOLUTION_SANDBOX_ROOT=/evolution-sandbox")) {
        if (-not $compose.Contains($evidence)) {
            throw "Compose evolution worker contract is missing: $evidence"
        }
    }
    if (-not $dockerfile.Contains("FROM agent_runtime") -or
        -not $dockerfile.Contains("evolution.temporal.worker")) {
        throw "Evolution worker must extend the verified Runtime image and run the governed worker."
    }
}

function Assert-CleanCloneSourcesTracked {
    $required = @(
        "frontend/lib/backend.ts",
        "frontend/lib/request-intent.ts",
        "frontend/lib/sse.ts",
        "src/RatsNestPro-main/RatsNestPro-main/src/ratsnestpro/parts/__init__.py",
        "src/RatsNestPro-main/RatsNestPro-main/src/ratsnestpro/parts/selector.py",
        "src/RatsNestPro-main/RatsNestPro-main/src/ratsnestpro/data/process_capability.json",
        "src/evolution/contracts.py",
        "src/evolution/temporal/worker.py",
        "config/harness/invariants.v1.json",
        "contracts/evolution/v1/evolution.schema.json",
        "backend/src/main/resources/db/migration/V8__create_run_interactions.sql",
        "backend/src/main/resources/db/migration/V9__create_harness_evolution.sql",
        "backend/src/main/resources/db/migration/V10__add_harness_rollout_rollback_target.sql",
        "docker/postgres/bootstrap-control-plane.sql",
        "docker/Dockerfile.evolution"
    )
    Push-Location $projectRoot
    try {
        foreach ($relativePath in $required) {
            & git ls-files --error-unmatch -- $relativePath *> $null
            if ($LASTEXITCODE -ne 0) {
                throw "A clean clone would be missing required runtime source: $relativePath"
            }
        }
    }
    finally {
        Pop-Location
    }

    $serviceDockerfile = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "docker/Dockerfile.service")
    if (-not $serviceDockerfile.Contains("COPY src/evolution/ ./evolution/")) {
        throw "The Runtime image does not include the governed evolution package."
    }
    $dockerIgnore = Get-Content -Raw -LiteralPath (Join-Path $projectRoot ".dockerignore")
    if ($dockerIgnore -match "(?m)^data/?$") {
        throw "A broad data ignore would remove bundled runtime package data."
    }
}

function Invoke-KustomizeRender {
    param([Parameter(Mandatory = $true)][string] $RelativePath)

    $path = Join-Path $projectRoot $RelativePath
    $output = & kubectl kustomize $path
    if ($LASTEXITCODE -ne 0) {
        throw "Kustomize render failed: $RelativePath"
    }
    return $output -join "`n"
}

Get-Command kubectl -ErrorAction Stop *> $null
Assert-LocalWorkflowReferences
Assert-PowerShellSyntax
Assert-EvolutionWorkerContract
Assert-CleanCloneSourcesTracked

$base = Invoke-KustomizeRender "deploy/k8s/base"
[void] (Invoke-KustomizeRender "deploy/k8s/cells/primary-region")
$operations = Invoke-KustomizeRender "deploy/k8s/operations"
$canary = Invoke-KustomizeRender "deploy/k8s/overlays/harness-canary"

if (-not $operations.Contains("name: ratsnest-flyway-secrets")) {
    throw "Flyway Job must use its dedicated schema-owner Secret."
}
if (-not $base.Contains("name: RATSNEST_AGENT_RUNTIME_CANARY_URL") -or
    -not $base.Contains("http://ratsnest-agent-service-canary:8080")) {
    throw "Control plane is missing the explicit canary Runtime endpoint."
}
if (-not $base.Contains("name: RATSNEST_AGENT_RUNTIME_CANARY_GRPC_TARGET") -or
    -not $base.Contains("ratsnest-agent-service-canary:9090")) {
    throw "Control plane is missing the explicit canary gRPC endpoint."
}

$requiredCanaryEvidence = @(
    "name: ratsnest-agent-service-canary",
    "name: ratsnest-temporal-worker-canary",
    "name: RATSNEST_HARNESS_VERSION_ID",
    "name: RATSNEST_HARNESS_MANIFEST_DIGEST",
    "ratsnest.io/release-track: canary",
    "ratsnest-hardware-cell-01-canary"
)
foreach ($evidence in $requiredCanaryEvidence) {
    if (-not $canary.Contains($evidence)) {
        throw "Rendered canary overlay is missing: $evidence"
    }
}

Write-Output "infrastructure-static-gates-ok"
