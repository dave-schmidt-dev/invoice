#!/usr/bin/env bash
# Install the project's git hooks by pointing core.hooksPath at hooks/.
# Idempotent: safe to run multiple times.

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"

git -C "$repo_root" config core.hooksPath hooks

echo "[install-hooks] core.hooksPath set to 'hooks' — pre-commit PII guard is now active."
