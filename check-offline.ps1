# Run from any directory after installing the locked development environment.
[CmdletBinding()]
param(
    [string]$Python = (Join-Path $PSScriptRoot '.venv/Scripts/python.exe'),
    [string]$BaseTemp = (Join-Path ([System.IO.Path]::GetTempPath()) ('sql-agent-tests-' + [guid]::NewGuid().ToString('N')))
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $checkout = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\', '/')
    $testTemp = [System.IO.Path]::GetFullPath($BaseTemp)
    if ($testTemp -eq $checkout -or $testTemp.StartsWith($checkout + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Pytest temporary output must stay outside the checkout.'
    }
    if (Test-Path -LiteralPath $testTemp) { throw 'Use a fresh BaseTemp directory; pytest removes its previous contents.' }
    $node = (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $pythonExe = (Get-Command $Python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $suites = @(Get-ChildItem -Path 'src/sql_agent_lab/macsql', 'prototypes/workspace-ui' -Filter 'test_*.cjs' -File | Sort-Object FullName)
    foreach ($suite in $suites) {
        Write-Host "Running $($suite.Name)"
        & $node $suite.FullName
        if ($LASTEXITCODE -ne 0) { throw "$($suite.Name) failed ($LASTEXITCODE)" }
    }
    Write-Host "$($suites.Count) Node suites passed. Running Python offline tests."
    & $pythonExe -X utf8 -m pytest -q --basetemp $testTemp
    if ($LASTEXITCODE -ne 0) { throw "pytest failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}
