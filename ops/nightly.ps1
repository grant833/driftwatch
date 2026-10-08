# driftwatch nightly publish (run by Windows Task Scheduler; see ops/install-nightly.ps1).
#  1. Verify the ledger, write today's anchor + dashboard data (docker compose run publisher)
#  2. Commit ONLY anchors/ and docs/ and push to GitHub. The commit time on GitHub is the
#     independent timestamp that proves the predictions existed before their outcomes.
# Uses your normal git login on this PC; no tokens are stored anywhere new.
# Any failure is sent to your Telegram.

# "Continue": git and docker print progress on stderr, which "Stop" would treat as fatal.
# Failures are detected from exit codes instead.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Log = Join-Path $PSScriptRoot "nightly.log"
Set-Location $Root

function Log($msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
    Add-Content -Path $Log -Value $line
    Write-Output $line
}

function Notify($msg) {
    docker compose run --rm publisher notify "$msg" 2>&1 | Out-Null
}

# Keep the log from growing forever (last ~2000 lines).
if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 500KB)) {
    Get-Content $Log -Tail 2000 | Set-Content "$Log.tmp"; Move-Item -Force "$Log.tmp" $Log
}

try {
    Log "publish: start"
    # Docker Desktop may still be starting after a reboot or wake: wait up to 10 minutes.
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 10
    }
    if (-not $ready) { throw "Docker is not running" }

    $out = docker compose run --rm publisher 2>&1
    $out | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { throw "publisher exited with code $LASTEXITCODE" }

    git add -- anchors docs
    git diff --cached --quiet -- anchors docs
    if ($LASTEXITCODE -eq 0) { Log "publish: nothing changed"; exit 0 }

    $day = Get-Date -Format "yyyy-MM-dd"
    # The pathspec makes git commit only these folders, even if other files are staged.
    git commit -q -m "Nightly anchor and dashboard data $day" -- anchors docs 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

    git push -q 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        # Usually means GitHub has commits this PC doesn't. Rebase our data commit on top.
        git pull --rebase --autostash -q 2>&1 | ForEach-Object { Log "  $_" }
        if ($LASTEXITCODE -ne 0) { git rebase --abort 2>&1 | Out-Null; throw "git pull --rebase failed" }
        git push -q 2>&1 | ForEach-Object { Log "  $_" }
        if ($LASTEXITCODE -ne 0) { throw "git push failed" }
    }
    Log "publish: pushed"
}
catch {
    Log "publish: FAILED - $_"
    Notify "Nightly publish failed on the PC: $_ (see ops/nightly.log)"
    exit 1
}
