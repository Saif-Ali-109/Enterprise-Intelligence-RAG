#!/usr/bin/env bash
#
# Repository hygiene gate. Fails the build on the four things that would make
# this repository unsafe to publish, unusable for a clone, or dishonest about
# what it contains.
#
#   1. A tracked .env                                   — secrets in Git
#   2. Third-party page content                        — FR-032, SC-015
#   3. A provider key pattern in tracked content        — FR-042
#   4. A machine-specific absolute path in a tracked file — path rule
#
# Why a script and not a review step: a rule that is only written down is a rule
# that gets broken under time pressure. plan.md already argues this for the two
# JSON Schemas; the same argument applies here. Each check below is a gate, not
# a note, and `make check` runs it.
#
# All paths are repo-relative. The repository root is derived from this script's
# own location, which is the one path that is correct by construction.
#
# Usage:  scripts/check_repo_hygiene.sh
# Exit:   0 clean, 1 one or more violations.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Colour only when writing to a terminal, so CI logs stay clean.
if [[ -t 1 ]]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BOLD=""; RESET=""
fi

violations=0

fail() {
  printf '%sFAIL%s  %s\n' "$RED" "$RESET" "$1"
  shift
  for line in "$@"; do
    printf '        %s\n' "$line"
  done
  violations=$((violations + 1))
}

pass() {
  printf '%sok  %s  %s%s\n' "$GREEN" "$RESET" "$1" "${2:+  $2}"
}

# Tracked files only. An untracked scratch file is not published, and scanning
# it would fail a clone before its author had a chance to remove it.
#
# `-z` and `tr '\0' '\n'` together, deliberately. `-z` is the correct way to list
# paths (a filename may contain a newline), but its output is NUL-separated, and
# every `grep` below then classifies the stream as binary and prints only
# "binary file matches" — no matching lines, and a `while read` loop gets one
# iteration holding the entire concatenated list, so `[[ -f $file ]]` is false
# for every element and the loop body never runs.
#
# That failure mode is the worst kind for a gate: it reports `ok` while
# scanning nothing, so a planted `.env` with a real key, or a committed scraped
# page, passes. The `-z` flag is kept for correctness of the listing and the NULs
# are converted immediately, at the one place every consumer shares.
tracked_files() {
  git ls-files -z | tr '\0' '\n'
}

printf '%sRepository hygiene%s  %s\n\n' "$BOLD" "$RESET" "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 1. No tracked .env
# ---------------------------------------------------------------------------
# `.env.example` is the template and is deliberately tracked. Any other `.env*`
# is a filled-in environment file and must never be committed (FR-042).
env_violations="$(tracked_files \
  | grep -E '(^|/)\.env($|\.)' \
  | grep -vE '(^|/)\.env\.example$' || true)"

if [[ -n "$env_violations" ]]; then
  fail "a .env file is tracked" \
    "Secrets reach Git history even if the file is later deleted." \
    "$(printf '%s' "$env_violations" | sed 's/^/  /')" \
    "" \
    "  Fix:  git rm --cached <file>  and add the pattern to .gitignore"
else
  pass "no tracked .env"
fi

# ---------------------------------------------------------------------------
# 2. No third-party page content
# ---------------------------------------------------------------------------
# FR-032: the corpus is ingested by URL and is never shipped. A committed HTML
# file, or a saved page, is a licence problem regardless of how it got there.
content_violations="$(tracked_files \
  | grep -E '\.(html|htm|xhtml|mhtml|pdf|epub)$' || true)"

if [[ -n "$content_violations" ]]; then
  fail "third-party page content is tracked" \
    "This repository ingests documentation by URL and must never package it (FR-032)." \
    "$(printf '%s' "$content_violations" | sed 's/^/  /')" \
    "" \
    "  If one of these is genuinely first-party (for example a licence file)," \
    "  rename it or add a specific negation to .gitignore and note why here."
else
  pass "no third-party page content"
fi

# The .gitignore negation for the licence's own markup, if any, is checked
# above by extension. This catches the subtler case: a large blob of publisher
# prose committed under a data or text extension.
large_violations="$(
  tracked_files \
    | grep -E '\.(txt|md|json|jsonl|ndjson)$' \
    | grep -vE '^(LICENSE|README\.md|specs/|docs/|data/source_manifest\.json|evaluation/golden_questions\.json)' \
    | while IFS= read -r file; do
        [[ -f "$file" ]] || continue
        size=$(wc -c <"$file")
        [[ "$size" -gt 262144 ]] && printf '%s (%s bytes)\n' "$file" "$size"
      done || true
)"

if [[ -n "$large_violations" ]]; then
  fail "a tracked data or text file is implausibly large" \
    "A 256KB documentation blob is usually a scraped page committed by mistake." \
    "$(printf '%s' "$large_violations" | sed 's/^/  /')"
else
  pass "no oversized text or data blobs"
fi

# ---------------------------------------------------------------------------
# 3. No provider key pattern in tracked content
# ---------------------------------------------------------------------------
# Pattern shapes only — no real key is reproduced here, and none is read from
# the environment. Matching on the prefix rather than the whole value means a
# truncated or redacted key is still caught.
key_violations="$(
  tracked_files \
    | grep -vE '(^\.env\.example$|check_repo_hygiene\.sh$|^\.gitignore$)' \
    | while IFS= read -r file; do
        [[ -f "$file" ]] || continue
        grep -nE '(gsk_[A-Za-z0-9]{20,}|pcsk_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{32,}|AIza[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|postgres(ql)?://[^:[:space:]]+:[^@[:space:]]+@)' "$file" 2>/dev/null \
          | sed "s|^|  $file:|"
      done || true
)"

if [[ -n "$key_violations" ]]; then
  fail "a provider key pattern appears in tracked content" \
    "Treat any of these as compromised: rotate it before doing anything else." \
    "$(printf '%s' "$key_violations" | head -20)" \
    "" \
    "  The .env.example template and this script are excluded; nothing else should match."
else
  pass "no provider key patterns"
fi

# ---------------------------------------------------------------------------
# 4. No machine-specific absolute path in a tracked file
# ---------------------------------------------------------------------------
# A path that depends on where the repository happens to be cloned breaks every
# other clone. The literal placeholder forms (`/home/<user>/`, `/Users/<name>/`)
# are the rule text itself and are therefore excluded by construction.
#
# The Windows drive pattern requires a boundary before the letter. Without it
# `[A-Za-z]:\\` matches the tail of any word ending in a capital letter followed
# by a backslash — which is why `"CONTEXT:\n"` in a Python string was reported
# as a path, since its final `T:\` is a valid drive-letter shape. A rule that
# fires on ordinary code gets disabled, and a disabled rule protects nothing.
# `(^|[^A-Za-z0-9_])` is the boundary; a genuine `C:\Users\...` has one.
path_violations="$(
  tracked_files \
    | grep -vE '^scripts/check_repo_hygiene\.sh$' \
    | while IFS= read -r file; do
        [[ -f "$file" ]] || continue
        grep -nE '(/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/|(^|[^A-Za-z0-9_])[A-Za-z]:\\[^ ]|/mnt/[a-z]/)' "$file" 2>/dev/null \
          | grep -vE '(/home/<|/Users/<)' \
          | sed "s|^|  $file:|"
      done || true
)"

if [[ -n "$path_violations" ]]; then
  fail "a machine-specific absolute path appears in a tracked file" \
    "Rewrite as a path relative to the repository root (tasks.md, 'Paths in code and specs')." \
    "$(printf '%s' "$path_violations" | head -20)"
else
  pass "no machine-specific absolute paths"
fi

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
echo
if [[ "$violations" -gt 0 ]]; then
  printf '%s%d hygiene check(s) failed.%s\n\n' "$RED$BOLD" "$violations" "$RESET"
  exit 1
fi

printf '%sAll hygiene checks passed.%s\n' "$GREEN$BOLD" "$RESET"
