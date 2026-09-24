param(
    [ValidateSet("brave", "chrome", "chromium", "edge", "firefox", "librewolf", "opera", "opera_gx", "vivaldi")]
    [string]$BrowserCookie = ""
)
$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $PSScriptRoot ".venv\bin\python.exe" }
if (-not (Test-Path -LiteralPath $python)) { throw "Run setup.ps1 first." }
$arguments = @("-m", "foodfinder", "login")
if ($BrowserCookie) { $arguments += @("--browser-cookie", $BrowserCookie) }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw "Instagram login failed. Resolve any Instagram verification challenge and try again later." }
