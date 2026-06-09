# Build premarket + normalizer into bin/; runtime logs go to bin/LOGS/.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bin = Join-Path $root "bin"
$logs = Join-Path $bin "LOGS"

New-Item -ItemType Directory -Force -Path $bin, $logs | Out-Null

Push-Location $root
try {
    go build -o (Join-Path $bin "premarket.exe") ./cmd/premarket
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    go build -o (Join-Path $bin "normalizer.exe") ./cmd/normalizer
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "built:"
    Write-Host "  $(Join-Path $bin 'premarket.exe')"
    Write-Host "  $(Join-Path $bin 'normalizer.exe')"
    Write-Host "logs: $logs"
}
finally {
    Pop-Location
}
