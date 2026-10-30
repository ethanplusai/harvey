# Pushes local-only ops work to the PRIVATE backup repo gaurkargos/harvey-ops.
#
# The local pre-push guard (.git/hooks/pre-push) blocks ops/* branches, any
# history containing c948575, and new reports/ commits on EVERY remote - it is
# deliberately left untouched and remains the enforcement for all manual
# pushes. This script is the ONLY sanctioned path to the private backup: it
# verifies via gh that the exact target repo exists and is PRIVATE, then pushes
# with --no-verify (the guard's own documented bypass) to the remote it itself
# registered from the constant below. It never takes a remote as an argument,
# so it cannot target anything public.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ops-backup.ps1
# PS 5.1 note: native stderr is left unredirected on purpose - under
# $ErrorActionPreference='Stop' a redirected stderr line becomes a
# terminating NativeCommandError. Failures are caught via $LASTEXITCODE.
$ErrorActionPreference = 'Continue'
$repo       = 'gaurkargos/harvey-ops'
$remoteName = 'opsbackup'

Set-Location (Join-Path $PSScriptRoot '..')

# 1. The target must exist and be private - check before anything else.
$json = gh repo view $repo --json visibility
if ($LASTEXITCODE -ne 0 -or -not $json) {
    throw "gh cannot see $repo (missing or not authed); refusing to push."
}
if (($json | ConvertFrom-Json).visibility -ne 'PRIVATE') {
    throw "$repo is not PRIVATE; refusing to push."
}

# 2. Register the remote if needed (URL is derived from the constant above).
$have = @(git remote)
if (-not ($have -contains $remoteName)) {
    git remote add $remoteName "https://github.com/$repo.git"
    if ($LASTEXITCODE -ne 0) { throw "git remote add failed" }
}

# 3. Push main first (so it becomes the default branch), then every ops/* branch.
$branches = @('main') + @(git for-each-ref --format='%(refname:short)' refs/heads/ops/)
foreach ($b in $branches) {
    git push --no-verify $remoteName "${b}:refs/heads/${b}"
    if ($LASTEXITCODE -ne 0) { throw "push failed: $b" }
    Write-Host "pushed ${b}"
}

# 4. Keep the default branch pinned (idempotent).
gh repo edit $repo --default-branch main | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not set default branch on $repo" }
Write-Host "ops backup complete: $branches -> $repo (private)"
