$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$venv = Join-Path $repo ".venv"
$probeCode = 'import sys,sysconfig; print(f"{sys.version_info.major}.{sys.version_info.minor}|{sysconfig.get_platform()}")'
$candidates = [System.Collections.Generic.List[object]]::new()

# Reuse an existing supported environment first so setup can be rerun safely.
foreach ($relativePath in @(".venv\Scripts\python.exe", ".venv\bin\python.exe")) {
    $existing = Join-Path $repo $relativePath
    if (Test-Path -LiteralPath $existing) {
        $candidates.Add([pscustomobject]@{ Executable = $existing; Arguments = @() })
    }
}

# Prefer a normal Python install managed by the Windows Python launcher.
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    foreach ($version in @("3.12", "3.11", "3.13")) {
        $candidates.Add([pscustomobject]@{ Executable = $launcher.Source; Arguments = @("-$version") })
    }
}

# Also consider PATH Python and every bundled Codex runtime. Filter each one by
# version and platform rather than trusting whichever executable is newest.
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    $candidates.Add([pscustomobject]@{ Executable = $python.Source; Arguments = @() })
}
$runtimeRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes"
Get-ChildItem -Path $runtimeRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object {
        $runtimePython = Join-Path $_.FullName "dependencies\python\python.exe"
        if (Test-Path -LiteralPath $runtimePython) {
            $candidates.Add([pscustomobject]@{ Executable = $runtimePython; Arguments = @() })
        }
    }

$selected = $null
$foundVersions = [System.Collections.Generic.List[string]]::new()
foreach ($candidate in $candidates) {
    $probeArguments = @($candidate.Arguments) + @("-c", $probeCode)
    $probe = & $candidate.Executable @probeArguments 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $probe) { continue }
    $parts = "$probe".Trim().Split("|")
    if ($parts.Count -ne 2) { continue }
    $foundVersions.Add("Python $($parts[0]) ($($parts[1]))")
    $versionParts = $parts[0].Split(".")
    if ($versionParts.Count -ne 2) { continue }
    $major = 0
    $minor = 0
    if (-not [int]::TryParse($versionParts[0], [ref]$major) -or -not [int]::TryParse($versionParts[1], [ref]$minor)) { continue }
    if ($major -eq 3 -and $minor -ge 11 -and $minor -le 13 -and $parts[1] -eq "win-amd64") {
        $selected = [pscustomobject]@{
            Executable = $candidate.Executable
            Arguments = @($candidate.Arguments)
            Version = $parts[0]
        }
        break
    }
}

if (-not $selected) {
    if ($foundVersions.Count) {
        $detected = $foundVersions -join ", "
        throw "No compatible 64-bit Windows Python 3.11-3.13 was found (detected: $detected). Install Python 3.12 from https://www.python.org/downloads/windows/ and rerun setup.ps1."
    }
    throw "No usable 64-bit Windows Python was found. Install Python 3.12 from https://www.python.org/downloads/windows/ and rerun setup.ps1."
}

$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = Join-Path $repo ".venv\bin\python.exe" }
$isExistingVenv = $selected.Executable -eq $venvPython
if (-not $isExistingVenv) {
    if (Test-Path -LiteralPath $venv) {
        throw "The existing .venv uses an unsupported Python version. Remove the .venv folder, then rerun setup.ps1."
    }
    $createArguments = @($selected.Arguments) + @("-m", "venv", $venv)
    & $selected.Executable @createArguments
    if ($LASTEXITCODE -ne 0) { throw "Could not create a virtual environment with Python $($selected.Version)." }
    $venvPython = Join-Path $repo ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = Join-Path $repo ".venv\bin\python.exe" }
}
Write-Host "Using Python $($selected.Version) from $venvPython"
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to prepare pip." }
# CPU-only PyTorch keeps setup usable without a CUDA-enabled GPU.
& $venvPython -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE -ne 0) { throw "Failed to install CPU-only PyTorch. Retry setup after checking your network connection." }
& $venvPython -m pip install -r (Join-Path $repo "requirements.txt") -r (Join-Path $repo "requirements-ocr.txt")
if ($LASTEXITCODE -ne 0) { throw "Failed to install application dependencies." }
Write-Host "Setup finished. Next run: powershell -ExecutionPolicy Bypass -File .\login-instagram.ps1"
