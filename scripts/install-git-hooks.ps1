<#
.SYNOPSIS
  Activate the versioned git hooks for this repo (currently: the immutable-
  baseline pre-push guard).
.DESCRIPTION
  Points core.hooksPath at scripts/hooks so scripts/hooks/pre-push runs before
  every push. Repo-level config -> applies to all worktrees. Idempotent.

  There are no other hooks in .git/hooks in this repo, so switching hooksPath
  loses nothing. If you later add hooks, put them in scripts/hooks/ too (git
  reads ONLY hooksPath once it is set).

  Bypass a single push:  HF_SKIP_BASELINE_GUARD=1 git push ...
  Undo:                  git config --unset core.hooksPath
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repo = (git rev-parse --show-toplevel).Trim()
$hooksDir = Join-Path $repo 'scripts/hooks'
if (-not (Test-Path (Join-Path $hooksDir 'pre-push'))) {
    throw "expected hook not found: $hooksDir/pre-push"
}

git -C $repo config core.hooksPath 'scripts/hooks'

# Best-effort exec bit (no-op on Windows filesystems; matters if this clone is
# ever used from WSL / a POSIX checkout).
try { git -C $repo update-index --chmod=+x scripts/hooks/pre-push 2>$null } catch {}

Write-Host "core.hooksPath -> scripts/hooks" -ForegroundColor Green
Write-Host "pre-push immutable-baseline guard is now active for every worktree." -ForegroundColor Green
Write-Host "Bypass one push with:  `$env:HF_SKIP_BASELINE_GUARD=1; git push; `$env:HF_SKIP_BASELINE_GUARD=`$null" -ForegroundColor DarkGray
