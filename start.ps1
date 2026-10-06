param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$parserPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $parserPython)) {
    throw 'Virtual environment missing. Run: py -3.14 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.lock'
}
Write-Host "Fast Parser: http://127.0.0.1:$Port — Ctrl+C to stop"
& $parserPython -m fast_parser serve --port $Port
