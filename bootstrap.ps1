# bootstrap.ps1 - install, upgrade, or remove coding-agent in the current project on Windows (PowerShell 5.1 or later).
#
# Install or upgrade, from the project directory:
#   irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1 | iex
#
# With installer arguments (uninstall, vendor, add new hooks, ...):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1))) --uninstall
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1))) --add-new-hooks
#
# Environment:
#   CODING_AGENT_REF     commit to install (default: the pinned commit below)
#   CODING_AGENT_OWNER   repository owner (default: dragonwar000)
#   CODING_AGENT_REPO    repository name (default: coding-agent)
#   CODING_AGENT_PYTHON  interpreter command (default: python, then python3, then py)
#   GH_TOKEN             token for a private repository; a public one needs none
#
# Run on Windows 11 with Windows PowerShell 5.1: see the README section "Windows".
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$InstallArgs)

$ErrorActionPreference = 'Stop'

# The code commit this script installs. Kept equal to PINNED_REF in bootstrap.sh.
$PinnedRef = '272396a0451b9f0e0f0f83a2b5f7e71904b3112d'
$Owner = if ($env:CODING_AGENT_OWNER) { $env:CODING_AGENT_OWNER } else { 'dragonwar000' }
$Repo = if ($env:CODING_AGENT_REPO) { $env:CODING_AGENT_REPO } else { 'coding-agent' }
$Ref = if ($env:CODING_AGENT_REF) { $env:CODING_AGENT_REF } else { $PinnedRef }

function Test-Native {
    # True when the command exits 0, with its output discarded. Windows PowerShell 5.1 under 'Stop' turns redirected
    # stderr of a native command into a terminating error, so the probe runs under 'Continue' and reads the exit code.
    param([string]$Command, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    try { & $Command @Arguments *> $null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
}

function Find-Python {
    $names = if ($env:CODING_AGENT_PYTHON) { @($env:CODING_AGENT_PYTHON) } else { @('python', 'python3', 'py') }
    foreach ($name in $names) {
        # -All: the Microsoft Store alias can sit ahead of a real Python on PATH. It answers `python` without being Python.
        foreach ($found in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            if (Test-Native $found.Source @('-c', 'import sys; sys.exit(sys.version_info < (3, 11))')) { return $found.Source }
        }
    }
    throw 'coding-agent: Python 3.11 or later was not found. Install it from python.org (tick "Add python.exe to PATH"), open a new terminal, then run this again.'
}

$Python = Find-Python
Write-Host "[bootstrap] python: $Python"

if (-not (Test-Native $Python @('-c', 'import yaml'))) {
    Write-Host '[bootstrap] installing pyyaml'
    & $Python -m pip install --quiet pyyaml
    if ($LASTEXITCODE -ne 0) { throw "coding-agent: could not install pyyaml. Run: $Python -m pip install pyyaml" }
}

$Target = (Get-Location).Path
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('coding-agent-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Tmp | Out-Null
$PreviousPythonPath = $env:PYTHONPATH
try {
    $Zip = Join-Path $Tmp 'src.zip'
    $Headers = @{}
    if ($env:GH_TOKEN) { $Headers['Authorization'] = "token $($env:GH_TOKEN)" }
    Write-Host "[bootstrap] downloading $Owner/$Repo@$Ref"
    Invoke-WebRequest -Uri "https://codeload.github.com/$Owner/$Repo/zip/$Ref" -OutFile $Zip -Headers $Headers -UseBasicParsing
    Expand-Archive -Path $Zip -DestinationPath $Tmp
    $Source = Get-ChildItem -Path $Tmp -Directory | Select-Object -First 1
    $Package = Join-Path (Join-Path $Source.FullName 'src') 'coding_agent'
    if (-not $Source -or -not (Test-Path $Package)) { throw 'coding-agent: the archive has no src/coding_agent' }
    $env:PYTHONPATH = Join-Path $Source.FullName 'src'
    Write-Host "[bootstrap] target: $Target"
    & $Python -m coding_agent.project_install --project $Target @InstallArgs
    if ($LASTEXITCODE -ne 0) { throw "coding-agent: the installer exited with code $LASTEXITCODE" }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}
