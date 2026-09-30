param(
    [switch]$IgnoreCooldown
)
$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $PSScriptRoot ".venv\bin\python.exe" }
if (-not (Test-Path -LiteralPath $python)) { throw "Run setup.ps1 first." }
Start-Process "http://127.0.0.1:8000"
$arguments = @("-u", "-X", "utf8", "-m", "foodfinder", "serve")
if ($IgnoreCooldown) { $arguments += "--ignore-cooldown" }
& $python @arguments
