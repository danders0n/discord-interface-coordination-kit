# Update the bot (docs/PLAN.md, *Code and tooling*, *Updates*): back the database up, pull, install, restart.
# The migrations run when the bot starts. Run from the bot's folder in an elevated PowerShell.
param(
    [string]$Service = "EventBot"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

& $python -m app check-config
if ($LASTEXITCODE -ne 0) { throw "The current .env is invalid; fix it before updating." }

& $python -m app backup
if ($LASTEXITCODE -ne 0) { throw "The backup failed; nothing was updated." }

git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "git pull failed; nothing was updated." }

& $python -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed; the old version is still running." }

& $python -m app check-config
if ($LASTEXITCODE -ne 0) { throw "The new version needs changes in .env (see .env.example); the old version is still running." }

nssm restart $Service
Write-Host "Updated and restarted $Service. Watch data\logs\bot.log and the staff log."
