[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string] $Context,

    [string] $Namespace = "ratsnest",

    [ValidateRange(30, 1800)]
    [int] $TimeoutSeconds = 600,

    [switch] $DatabaseTrafficDrainedToCanary
)

$ErrorActionPreference = "Stop"
$canaryTargets = @(
    @{ Deployment = "ratsnest-agent-service-canary"; Container = "api" },
    @{ Deployment = "ratsnest-temporal-worker-canary"; Container = "worker" }
)
$stableTargets = @(
    @{ Deployment = "ratsnest-agent-service"; Container = "api" },
    @{ Deployment = "ratsnest-temporal-worker"; Container = "worker" }
)

function Invoke-Kubectl {
    param([Parameter(Mandatory = $true)][string[]] $CommandArguments)

    $result = & kubectl @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl failed: kubectl $($CommandArguments -join ' ')"
    }
    return $result
}

function Get-ContainerValue {
    param(
        [Parameter(Mandatory = $true)] $Deployment,
        [Parameter(Mandatory = $true)][string] $ContainerName,
        [Parameter(Mandatory = $true)][string] $EnvironmentName
    )

    $container = $Deployment.spec.template.spec.containers |
        Where-Object { $_.name -eq $ContainerName } |
        Select-Object -First 1
    if ($null -eq $container) {
        throw "Container $ContainerName is missing from $($Deployment.metadata.name)."
    }
    $entry = $container.env |
        Where-Object { $_.name -eq $EnvironmentName } |
        Select-Object -First 1
    if ($null -eq $entry -or [string]::IsNullOrWhiteSpace($entry.value)) {
        throw "$EnvironmentName is missing from $($Deployment.metadata.name)."
    }
    return [string] $entry.value
}

function New-PromotionPatch {
    param(
        [Parameter(Mandatory = $true)][string] $Container,
        [Parameter(Mandatory = $true)][string] $PreviousVersion
    )

    $imageDigest = $Image.Substring($Image.LastIndexOf("@") + 1)
    return @{
        metadata = @{
            annotations = @{
                "ratsnest.io/harness-version" = $VersionId
                "ratsnest.io/previous-harness-version" = $PreviousVersion
                "ratsnest.io/runtime-image-digest" = $imageDigest
                "ratsnest.io/harness-manifest-digest" = $ManifestDigest
            }
        }
        spec = @{
            template = @{
                metadata = @{
                    labels = @{
                        "ratsnest.io/release-track" = "stable"
                        "ratsnest.io/harness-version" = $VersionId
                    }
                    annotations = @{
                        "ratsnest.io/runtime-image-digest" = $imageDigest
                        "ratsnest.io/harness-manifest-digest" = $ManifestDigest
                    }
                }
                spec = @{
                    containers = @(
                        @{
                            name = $Container
                            image = $Image
                            env = @(
                                @{ name = "RATSNEST_HARNESS_VERSION_ID"; value = $VersionId },
                                @{ name = "RATSNEST_HARNESS_CHANNEL"; value = "stable" },
                                @{ name = "RATSNEST_HARNESS_MANIFEST_DIGEST"; value = $ManifestDigest }
                            )
                        }
                    )
                }
            }
        }
    } | ConvertTo-Json -Depth 12 -Compress
}

Get-Command kubectl -ErrorAction Stop *> $null
$knownContext = & kubectl config get-contexts $Context -o name
if ($LASTEXITCODE -ne 0 -or $knownContext -notcontains $Context) {
    throw "Kubernetes context does not exist: $Context"
}
if (-not $DatabaseTrafficDrainedToCanary) {
    throw "Set the server-governed rollout to 100% canary before patching stable Deployments, then pass -DatabaseTrafficDrainedToCanary."
}

$canaryDeployments = @()
foreach ($target in $canaryTargets) {
    $raw = Invoke-Kubectl @(
        "--context", $Context, "-n", $Namespace,
        "get", "deployment", $target.Deployment, "-o", "json"
    )
    $deployment = ($raw -join "`n") | ConvertFrom-Json
    if ([int] $deployment.status.readyReplicas -lt 1) {
        throw "Canary is not ready: $($target.Deployment)"
    }
    $canaryDeployments += $deployment
}

$apiContainer = $canaryDeployments[0].spec.template.spec.containers |
    Where-Object { $_.name -eq "api" } | Select-Object -First 1
$workerContainer = $canaryDeployments[1].spec.template.spec.containers |
    Where-Object { $_.name -eq "worker" } | Select-Object -First 1
$Image = [string] $apiContainer.image
if ($Image -notmatch '^\S+@sha256:[0-9a-f]{64}$' -or $workerContainer.image -ne $Image) {
    throw "Canary API and worker must use the same immutable image digest."
}
$VersionId = Get-ContainerValue $canaryDeployments[0] "api" "RATSNEST_HARNESS_VERSION_ID"
if ($VersionId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$') {
    throw "Canary Harness version ID is not a Kubernetes-safe release ID: $VersionId"
}
$ManifestDigest = Get-ContainerValue $canaryDeployments[0] "api" "RATSNEST_HARNESS_MANIFEST_DIGEST"
if ($ManifestDigest -notmatch '^[0-9a-f]{64}$') {
    throw "Canary manifest digest is invalid."
}
if ((Get-ContainerValue $canaryDeployments[1] "worker" "RATSNEST_HARNESS_VERSION_ID") -ne $VersionId -or
    (Get-ContainerValue $canaryDeployments[1] "worker" "RATSNEST_HARNESS_MANIFEST_DIGEST") -ne $ManifestDigest) {
    throw "Canary API and worker do not identify the same Harness release."
}

if (-not $PSCmdlet.ShouldProcess(
        "$Context/$Namespace",
        "Promote verified canary Harness $VersionId to stable")) {
    return
}

$patched = @()
try {
    foreach ($target in $stableTargets) {
        $raw = Invoke-Kubectl @(
            "--context", $Context, "-n", $Namespace,
            "get", "deployment", $target.Deployment, "-o", "json"
        )
        $stable = ($raw -join "`n") | ConvertFrom-Json
        $previousVersion = Get-ContainerValue `
            $stable $target.Container "RATSNEST_HARNESS_VERSION_ID"
        $patch = New-PromotionPatch `
            -Container $target.Container `
            -PreviousVersion $previousVersion
        Invoke-Kubectl @(
            "--context", $Context, "-n", $Namespace,
            "patch", "deployment", $target.Deployment,
            "--type", "strategic", "--patch=$patch"
        ) *> $null
        $patched += $target.Deployment
    }

    foreach ($target in $stableTargets) {
        Invoke-Kubectl @(
            "--context", $Context, "-n", $Namespace,
            "rollout", "status", "deployment/$($target.Deployment)",
            "--timeout=$($TimeoutSeconds)s"
        )
    }
}
catch {
    foreach ($deployment in $patched) {
        & kubectl --context $Context -n $Namespace rollout undo `
            "deployment/$deployment" *> $null
    }
    throw
}

Write-Output "stable-promoted version_id=$VersionId image=$Image manifest=$ManifestDigest"
Write-Output "canary-kept-running reason=drain-in-flight-runs-before-scale-down"
