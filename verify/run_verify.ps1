# V.O.I.D laptop verification runner.
# Creates an isolated .venv inside the project (touches nothing global),
# installs the project's own free dependencies, and runs the four checks.
# All console output is tee'd to verify\setup_log.txt.
# Reversible: delete the .venv folder to undo everything this sets up.

$ErrorActionPreference = "Continue"
$proj = "C:\Users\nanda\OneDrive\Desktop\Studies\AI\Assistant\V.O.I.D"
Set-Location $proj

$log = Join-Path $proj "verify\setup_log.txt"
"=== V.O.I.D laptop verification $(Get-Date -Format o) ===" | Out-File -FilePath $log -Encoding utf8

# Pick a Python launcher.
$python = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }

if (-not $python) {
    "ERROR: No 'python' or 'py' launcher found on PATH." | Tee-Object -FilePath $log -Append
    "VERIFY_SETUP_FAILED" | Tee-Object -FilePath $log -Append
    exit 1
}

"Using launcher: $python" | Tee-Object -FilePath $log -Append
& $python --version 2>&1 | Tee-Object -FilePath $log -Append

if (-not (Test-Path ".venv")) {
    "Creating .venv ..." | Tee-Object -FilePath $log -Append
    & $python -m venv .venv 2>&1 | Tee-Object -FilePath $log -Append
} else {
    ".venv already exists; reusing it." | Tee-Object -FilePath $log -Append
}

$venvPy = Join-Path $proj ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    "ERROR: venv python not found at $venvPy" | Tee-Object -FilePath $log -Append
    "VERIFY_SETUP_FAILED" | Tee-Object -FilePath $log -Append
    exit 1
}

"Upgrading pip ..." | Tee-Object -FilePath $log -Append
& $venvPy -m pip install --upgrade pip 2>&1 | Tee-Object -FilePath $log -Append

"Installing requirements (this can take a few minutes for PySide6) ..." | Tee-Object -FilePath $log -Append
& $venvPy -m pip install -r requirements.txt 2>&1 | Tee-Object -FilePath $log -Append

"Running verification checks ..." | Tee-Object -FilePath $log -Append
& $venvPy verify\laptop_check.py 2>&1 | Tee-Object -FilePath $log -Append

"=== RUN COMPLETE ===" | Tee-Object -FilePath $log -Append
"VERIFY_SETUP_DONE" | Tee-Object -FilePath $log -Append
