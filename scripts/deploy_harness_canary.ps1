[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string] $Context,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')]
    [string] $VersionId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\S+@sha256:[0-9a-f]{64}$')]
    [string] $Image,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string] $ManifestDigest,

    [string] $Namespace = "ratsnest",

    [ValidateRange(30, 1800)]
    [int] $TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$targets = @(
    @{ Deployment = "ratsnest-agent-service-canary"; Container = "api" },
    @{ Deployment = "ratsnest-temporal-worker-canary"; Container = "worker" }
)

function Invoke-Kubectl {
    param([Parameter(Mandatory = $true)][string[]] $CommandArguments)

    & kubectl @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl failed: kubectl $($CommandArguments -join ' ')"
    }
}

function New-ReleasePatch {
    param(
        [Parameter(Mandatory = $true)][string] $Container,
        [Parameter(Mandatory = $true)][string] $Channel
    )

    $imageDigest = $Image.Substring($Image.LastIndexOf("@") + 1)
    return @{
        metadata = @{
            annotations = @{
                "ratsnest.io/harness-version" = $VersionId
                "ratsnest.io/runtime-image-digest" = $imageDigest
                "ratsnest.io/harness-manifest-digest" = $ManifestDigest
            }
        }
        spec = @{
            template = @{
                metadata = @{
                    labels = @{
                        "ratsnest.io/release-track" = $Channel
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
                                @{ name = "RATSNEST_HARNESS_CHANNEL"; value = $Channel },
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

if (-not $PSCmdlet.ShouldProcess(
        "$Context/$Namespace",
        "Deploy Harness $VersionId to the isolated canary API and worker")) {
    return
}

try {
    foreach ($target in $targets) {
        $patch = New-ReleasePatch -Container $target.Container -Channel "canary"
        Invoke-Kubectl @(
            "--context", $Context, "-n", $Namespace,
            "patch", "deployment", $target.Deployment,
            "--type", "strategic", "--patch=$patch"
        )
    }

    foreach ($target in $targets) {
        Invoke-Kubectl @(
            "--context", $Context, "-n", $Namespace,
            "scale", "deployment/$($target.Deployment)", "--replicas=1"
        )
        Invoke-Kubectl @(
            "--context", $Context, "-n", $Namespace,
            "rollout", "status", "deployment/$($target.Deployment)",
            "--timeout=$($TimeoutSeconds)s"
        )
    }
}
catch {
    foreach ($target in $targets) {
        & kubectl --context $Context -n $Namespace scale `
            "deployment/$($target.Deployment)" --replicas=0 *> $null
    }
    throw
}

Write-Output "canary-ready version_id=$VersionId image=$Image manifest=$ManifestDigest"
