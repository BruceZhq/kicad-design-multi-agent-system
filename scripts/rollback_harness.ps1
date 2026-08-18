[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string] $Context,

    [ValidateSet("stable", "canary")]
    [string] $Channel = "stable",

    [string] $Namespace = "ratsnest",

    [ValidateRange(1, 2147483647)]
    [int] $Revision,

    [ValidateRange(30, 1800)]
    [int] $TimeoutSeconds = 600,

    [switch] $DatabaseTrafficDrained
)

$ErrorActionPreference = "Stop"
$suffix = if ($Channel -eq "canary") { "-canary" } else { "" }
$deployments = @(
    "ratsnest-agent-service$suffix",
    "ratsnest-temporal-worker$suffix"
)

function Invoke-Kubectl {
    param([Parameter(Mandatory = $true)][string[]] $CommandArguments)

    & kubectl @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl failed: kubectl $($CommandArguments -join ' ')"
    }
}

Get-Command kubectl -ErrorAction Stop *> $null
$knownContext = & kubectl config get-contexts $Context -o name
if ($LASTEXITCODE -ne 0 -or $knownContext -notcontains $Context) {
    throw "Kubernetes context does not exist: $Context"
}
if (-not $DatabaseTrafficDrained) {
    throw "Drain new Run routing away from this channel in the server-governed rollout before Kubernetes rollback, then pass -DatabaseTrafficDrained."
}

if (-not $PSCmdlet.ShouldProcess(
        "$Context/$Namespace",
        "Roll back the $Channel Harness API and worker Deployments")) {
    return
}

$errors = @()
foreach ($deployment in $deployments) {
    $arguments = @(
        "--context", $Context, "-n", $Namespace,
        "rollout", "undo", "deployment/$deployment"
    )
    if ($PSBoundParameters.ContainsKey("Revision")) {
        $arguments += "--to-revision=$Revision"
    }
    try {
        Invoke-Kubectl $arguments
    }
    catch {
        $errors += $_
    }
}
if ($errors.Count -gt 0) {
    throw "One or more rollback requests failed: $($errors -join '; ')"
}

foreach ($deployment in $deployments) {
    Invoke-Kubectl @(
        "--context", $Context, "-n", $Namespace,
        "rollout", "status", "deployment/$deployment",
        "--timeout=$($TimeoutSeconds)s"
    )
}

Write-Output "harness-rollback-complete channel=$Channel revision=$Revision"
