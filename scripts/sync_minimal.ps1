param(
    [switch]$Mirror,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pairs = @(
    @{
        Src = Join-Path $repoRoot "backend\app"
        Dst = Join-Path $repoRoot "Minimal-Server-Deploy\backend\app"
    },
    @{
        Src = Join-Path $repoRoot "frontend"
        Dst = Join-Path $repoRoot "Minimal-Server-Deploy\frontend"
    }
)

function Invoke-SafeRobocopy {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [switch]$UseMirror,
        [switch]$UseDryRun
    )

    if (-not (Test-Path $Source)) {
        throw "Source path not found: $Source"
    }
    if (-not (Test-Path $Target)) {
        New-Item -ItemType Directory -Path $Target -Force | Out-Null
    }

    $args = @(
        $Source,
        $Target,
        "*.*",
        "/E",
        "/FFT",
        "/R:1",
        "/W:1",
        "/NP",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS"
    )
    if ($UseMirror) { $args += "/MIR" }
    if ($UseDryRun) { $args += "/L" }

    Write-Host "Sync: $Source -> $Target"
    & robocopy @args | Out-Host
    $exitCode = $LASTEXITCODE
    if ($exitCode -ge 8) {
        throw "Robocopy failed ($exitCode): $Source -> $Target"
    }
}

foreach ($pair in $pairs) {
    Invoke-SafeRobocopy -Source $pair.Src -Target $pair.Dst -UseMirror:$Mirror -UseDryRun:$DryRun
}

Write-Host "Sync done"
