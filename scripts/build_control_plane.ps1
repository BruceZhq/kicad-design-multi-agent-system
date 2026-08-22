[CmdletBinding()]
param(
    [string] $BuildImage = "maven:3.9.12-eclipse-temurin-21@sha256:c3c9d3ac4ce8431a3995c0318b8d390f448e693dd4fabc16e9b68d2e1f3d7b46",
    [string] $RuntimeImage = "maven:3.9.12-eclipse-temurin-21@sha256:c3c9d3ac4ce8431a3995c0318b8d390f448e693dd4fabc16e9b68d2e1f3d7b46"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputImage = "kicad-design-multi-agent-system-control-plane:latest"

function Assert-LocalJava21Image {
    param([Parameter(Mandatory = $true)][string] $Image)

    docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Required base image is not local: $Image. Load or pull it explicitly before building."
    }

    $versionOutput = docker run --rm --entrypoint java $Image --version
    $versionText = $versionOutput -join "`n"
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch "(?m)^(openjdk|java) 21(?:\.|\s)") {
        throw "Base image does not provide Java 21: $Image"
    }
}

Push-Location $projectRoot
try {
    @($BuildImage, $RuntimeImage) | Select-Object -Unique | ForEach-Object {
        Assert-LocalJava21Image $_
    }

    New-Item -ItemType Directory -Force -Path ".build-cache\control-plane" *> $null

    $previousBuildImage = $env:RATSNEST_JAVA_BUILD_IMAGE
    $previousRuntimeImage = $env:RATSNEST_JAVA_RUNTIME_IMAGE
    $env:RATSNEST_JAVA_BUILD_IMAGE = $BuildImage
    $env:RATSNEST_JAVA_RUNTIME_IMAGE = $RuntimeImage

    docker compose --profile control-plane config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Compose validation failed."
    }

    docker compose --progress plain --profile control-plane build control_plane
    if ($LASTEXITCODE -ne 0) {
        throw "Control-plane build failed."
    }

    $entrypoint = docker image inspect $outputImage --format '{{json .Config.Entrypoint}}'
    if ($LASTEXITCODE -ne 0 -or $entrypoint -ne '["java","-jar","/app/app.jar"]') {
        throw "Built image has an unexpected entrypoint: $entrypoint"
    }

    Assert-LocalJava21Image $outputImage
    Write-Output "control-plane-build-ok image=$outputImage java=21"
}
finally {
    $env:RATSNEST_JAVA_BUILD_IMAGE = $previousBuildImage
    $env:RATSNEST_JAVA_RUNTIME_IMAGE = $previousRuntimeImage
    Pop-Location
}
