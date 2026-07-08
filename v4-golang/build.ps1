# Build premarket venue binaries + normalizer into bin/; runtime logs go to bin/LOGS/.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bin = Join-Path $root "bin"
$logs = Join-Path $bin "LOGS"

New-Item -ItemType Directory -Force -Path $bin, $logs | Out-Null

$targets = @(
    @{ Out = "premarket-india.exe"; Pkg = "./cmd/premarket-india" },
    @{ Out = "premarket-XCME.exe";  Pkg = "./cmd/premarket-xcme" },
    @{ Out = "premarket-XCBO.exe";  Pkg = "./cmd/premarket-xcbo" },
    @{ Out = "premarket-XNAS.exe";  Pkg = "./cmd/premarket-xnas" },
    @{ Out = "normalizer.exe";      Pkg = "./cmd/normalizer" }
)

$remove = @(
    "premarket.exe",
    "premarket-india.exe",
    "premarket-XCME.exe",
    "premarket-XCBO.exe",
    "premarket-XNAS.exe",
    "normalizer.exe"
)

Push-Location $root
try {
    foreach ($name in $remove) {
        $path = Join-Path $bin $name
        if (Test-Path $path) {
            Remove-Item -Force $path
        }
    }

    Write-Host "building..."
    foreach ($t in $targets) {
        $outPath = Join-Path $bin $t.Out
        go build -o $outPath $t.Pkg
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Write-Host "  $outPath"
    }
    Write-Host "logs: $logs"
}
finally {
    Pop-Location
}
