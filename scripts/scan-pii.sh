#!/usr/bin/env bash
# Full-tree PII scanner: scans every TRACKED file in the repo against
# hooks/pii-patterns.txt. Complementary to hooks/pre-commit, which only
# scans the staged diff at commit time — this script is a periodic /
# on-demand audit of everything already committed.
#
# Fail-closed by design: any error condition below (missing pattern file,
# empty pattern list, grep failure) blocks (exits non-zero) rather than
# silently allowing a clean result through.
#
# Read-only: never modifies any file.

set -euo pipefail

# ---------------------------------------------------------------------------
# HONESTY HEADER — read this before trusting a clean scan.
#
# SCOPE LIMIT: this scanner detects NAMES/TOKENS only, via literal/regex
# patterns in hooks/pii-patterns.txt. It CANNOT detect financial identifiers
# (retainer amounts, hourly rates, invoice numbers) because the sanctioned
# placeholder values (e.g. $100.00, 2026-0001) are digit-shape-identical to
# real values — no regex can distinguish a placeholder dollar amount or
# invoice number from a real one. The durable control for those is keeping
# operational files OUT of the repo (.gitignore) plus human review, NOT
# this scanner. A clean scan does NOT mean "PII-safe."
# ---------------------------------------------------------------------------
echo "[scan-pii] HONESTY HEADER: this scanner detects NAMES/TOKENS only, via" >&2
echo "[scan-pii] literal/regex patterns in hooks/pii-patterns.txt. It CANNOT" >&2
echo "[scan-pii] detect financial identifiers (retainer amounts, hourly rates," >&2
echo "[scan-pii] invoice numbers) because sanctioned placeholders (e.g. \$100.00," >&2
echo "[scan-pii] 2026-0001) are digit-shape-identical to real values -- no regex" >&2
echo "[scan-pii] can tell them apart. The durable control for those is keeping" >&2
echo "[scan-pii] operational files OUT of the repo (.gitignore) plus human review," >&2
echo "[scan-pii] NOT this scanner. A clean scan does NOT mean \"PII-safe\"." >&2

repo_root="$(git rev-parse --show-toplevel)"
patterns="$repo_root/hooks/pii-patterns.txt"

if [[ ! -f "$patterns" ]]; then
    echo "[scan-pii] BLOCKED: pattern file missing: $patterns" >&2
    echo "[scan-pii] Cannot verify tracked content is free of PII without it." >&2
    exit 1
fi

# Strip blank lines and comments from the pattern file. Each remaining line
# is treated as a case-insensitive grep -E pattern. An empty pattern list
# means we cannot verify anything, so fail closed.
pattern_list="$(grep -Ev '^[[:space:]]*(#|$)' "$patterns" || true)"
if [[ -z "$pattern_list" ]]; then
    echo "[scan-pii] BLOCKED: $patterns has no active patterns (all blank/comments)." >&2
    echo "[scan-pii] Cannot verify tracked content is free of PII without it." >&2
    exit 1
fi

# All tracked files, excluding the pattern file itself (it legitimately
# contains the guarded strings). NUL-delimited to survive odd filenames.
cd "$repo_root"

overall_status=0
any_hits=0

while IFS= read -r -d '' file; do
    if [[ "$file" == "hooks/pii-patterns.txt" ]]; then
        continue
    fi
    # Skip files that no longer exist on disk (e.g. a tracked-but-deleted
    # path in an unusual worktree state) rather than letting grep error out.
    if [[ ! -f "$file" ]]; then
        continue
    fi

    set +e
    hits="$(grep -niE -f <(printf '%s\n' "$pattern_list") -- "$file")"
    grep_status=$?
    set -e

    if [[ $grep_status -ge 2 ]]; then
        echo "[scan-pii] BLOCKED: grep failed while scanning '$file' (exit $grep_status)." >&2
        echo "[scan-pii] Check $patterns for a malformed regex." >&2
        overall_status=1
        continue
    fi

    if [[ $grep_status -eq 0 && -n "$hits" ]]; then
        any_hits=1
        overall_status=1
        echo "" >&2
        echo "[scan-pii] MATCH in $file:" >&2
        printf '%s\n' "$hits" | sed "s|^|[scan-pii]   $file:|" >&2
    fi
done < <(git ls-files -z)

if [[ $overall_status -ne 0 ]]; then
    echo "" >&2
    if [[ $any_hits -eq 1 ]]; then
        echo "[scan-pii] BLOCKED: tracked files contain forbidden PII / sensitive patterns." >&2
    fi
    echo "[scan-pii] Rule: README.md -> \"No PII in the project\"." >&2
    echo "[scan-pii] Pattern list: $patterns" >&2
    exit 1
fi

echo "[scan-pii] Clean: no tracked file matched a pattern in $patterns." >&2
exit 0
