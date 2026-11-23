param(
    [int]$Port = 8780,
    [string]$Config = "$PSScriptRoot/.local/web-config.json"
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Config)) {
    throw 'Missing local config. Copy web-config.example.json to .local/web-config.json and set paths.'
}
$settings = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$launchArgs = @('-B', '-m', 'sql_agent_lab.macsql.live_ui',
    '--output-dir', $settings.output_dir, '--port', "$Port")
if ($settings.data_dir) { $launchArgs += @('--data-dir', $settings.data_dir) }
if ($settings.sources) { $launchArgs += @('--sources', $settings.sources) }
if ($settings.workspace_db) { $launchArgs += @('--workspace-db', $settings.workspace_db) }
if ($settings.metadata) { $launchArgs += @('--metadata', $settings.metadata) }
if ($settings.analysis_dir) { $launchArgs += @('--analysis-dir', $settings.analysis_dir) }
if ($settings.history) { $launchArgs += @('--history', $settings.history) }
foreach ($batch in $settings.recorded_batches) { $launchArgs += @('--recorded-batch', $batch) }
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:LANGSMITH_TRACING = 'false'
& $python @launchArgs
exit $LASTEXITCODE
