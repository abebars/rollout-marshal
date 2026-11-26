#!/usr/bin/env bash
# Builds app/spike/build/rm-spike-sdk.zip for task 1.2 (Lambda python3.13, arm64).
#
# The package is the app dependency group exactly as uv.lock pins it, hashes checked,
# installed from manylinux aarch64 wheels for CPython 3.13 (some are compiled: regex,
# charset-normalizer, iregexp-check), plus the handler and contracts/names.py. boto3 comes
# from the Lambda runtime. Writes only under app/spike/build/, which git ignores.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT="$HERE/build"
PKG="$OUT/package"
ZIP="$OUT/rm-spike-sdk.zip"
PLATFORM="aarch64-manylinux2014" # glibc 2.17 wheels; Lambda's python3.13 is Amazon Linux 2023
PYTHON_VERSION=$(cd "$ROOT" && uv run --frozen python -c 'from contracts import names; print(names.PYTHON_VERSION)')

rm -rf "$OUT"
mkdir -p "$PKG"

uv export --project "$ROOT" --frozen --only-group app --no-emit-project \
  --format requirements-txt -o "$OUT/requirements.txt" >/dev/null
uv pip install --quiet --target "$PKG" --python-platform "$PLATFORM" \
  --python-version "$PYTHON_VERSION" --only-binary :all: -r "$OUT/requirements.txt"

mkdir -p "$PKG/app/spike" "$PKG/contracts"
cp "$ROOT/app/__init__.py" "$PKG/app/"
cp "$ROOT/app/spike/__init__.py" "$ROOT/app/spike/handler.py" "$PKG/app/spike/"
cp "$ROOT/contracts/__init__.py" "$ROOT/contracts/names.py" "$PKG/contracts/"

# Every compiled module must be a Linux aarch64 build, and every wheel tag must fit.
bad=0
while IFS= read -r so; do
  if ! file "$so" | grep -q "ARM aarch64"; then
    echo "not an aarch64 ELF: ${so#"$PKG"/}" >&2
    bad=1
  fi
done < <(find "$PKG" -name '*.so')
if grep -h '^Tag:' "$PKG"/*.dist-info/WHEEL | grep -v -E 'py3-none-any|manylinux.*aarch64' >&2; then
  bad=1
fi
[ "$bad" -eq 0 ] || { echo "build: wrong platform in the package" >&2; exit 1; }

# A repeatable zip: fixed timestamps, sorted entries, no extra attributes. uv's lock file
# and the console scripts in bin/ are left out; Lambda needs neither.
find "$PKG" -exec touch -h -t 202611260000 {} +
(cd "$PKG" && find . -type f ! -name .lock ! -path './bin/*' | LC_ALL=C sort | zip -X -q "$ZIP" -@)

echo "built ${ZIP#"$ROOT"/} ($(du -k "$ZIP" | cut -f1) KiB)"
find "$PKG" -name '*.so' | sed "s|^$PKG/|  compiled: |" | LC_ALL=C sort
grep -h '^Tag:' "$PKG"/*.dist-info/WHEEL | grep aarch64 | LC_ALL=C sort -u | sed 's/^/  wheel /'
