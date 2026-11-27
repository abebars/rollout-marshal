#!/usr/bin/env bash
# Builds app/build/rm-answer.zip, rm-router.zip and rm-judge.zip for task 3.5's functions
# (Lambda python3.13, arm64). make build runs it before every plan and apply. What each zip
# holds, and why, is in app/build_zips.py. Writes only under app/build/, which git ignores.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)

cd "$ROOT"
exec uv run --frozen python "$HERE/build_zips.py"
