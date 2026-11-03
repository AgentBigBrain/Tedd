Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$entrypoint = Join-Path $PSScriptRoot "verify_release.py"
$configuredPython = $null
if (-not [string]::IsNullOrWhiteSpace($env:pythonLocation)) {
    $candidate = Join-Path $env:pythonLocation "python.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $configuredPython = $candidate
    }
}
if ($null -ne $configuredPython) {
    & $configuredPython $entrypoint @args
    exit $LASTEXITCODE
}

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $launcher) {
    & $launcher.Source -3 $entrypoint @args
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python) {
    & $python.Source $entrypoint @args
    exit $LASTEXITCODE
}

throw "Python 3.10 or newer is required to verify Tedd."
