# Run from any directory after installing the locked development environment.
[CmdletBinding()]
param(
    [string]$Python = (Join-Path $PSScriptRoot '.venv/Scripts/python.exe')
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $node = (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $pythonExe = (Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $suites = @(Get-ChildItem -Path 'src/sql_agent_lab/macsql', 'prototypes/workspace-ui' -Filter 'test_*.cjs' -File | Sort-Object FullName)
    foreach ($suite in $suites) {
        Write-Host "Running $($suite.Name)"
        & $node $suite.FullName
        if ($LASTEXITCODE -ne 0) { throw "$($suite.Name) failed ($LASTEXITCODE)" }
    }
    Write-Host "$($suites.Count) Node suites passed. Running Python offline tests."
    & $pythonExe -X utf8 -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "pytest failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}
