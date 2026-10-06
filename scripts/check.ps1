$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$parserTestDir = Join-Path (Get-Location) ('data\pytest-' + [guid]::NewGuid().ToString())
& .\.venv\Scripts\python.exe -m pytest -q --basetemp=$parserTestDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& .\.venv\Scripts\python.exe -m compileall -q fast_parser
exit $LASTEXITCODE
