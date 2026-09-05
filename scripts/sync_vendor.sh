#!/usr/bin/env bash
# Copyright (c) 2026 HOOX · PYNE · jango-blockchained
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Sync the live pynescript package into python_modules/ for Cloudflare deploy.
# Wrangler packages python_modules/ as "Vendored Modules" — it must stay in
# sync with the sibling pynescript repo (especially util/time_parts.py, compiler/).
#
# Usage:
#   ./scripts/sync_vendor.sh
#   PYNESCRIPT_SRC=/path/to/pyne/src/pynescript ./scripts/sync_vendor.sh
#   (repo may be cloned as pyne or pynescript)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/python_modules/pynescript"

# Prefer explicit env, then common sibling layouts (published clone name is often `pyne`)
CANDIDATES=(
  "${PYNESCRIPT_SRC:-}"
  "$ROOT/../pyne/src/pynescript"
  "$ROOT/../pynescript/src/pynescript"
  "$ROOT/../hoox-pyne/src/pynescript"
  "/mnt/data/home/jango/Git/pynescript/src/pynescript"
  "/home/jango/Git/pynescript/src/pynescript"
)

SRC=""
for c in "${CANDIDATES[@]}"; do
  if [[ -n "$c" && -f "$c/__init__.py" ]]; then
    SRC="$c"
    break
  fi
done

if [[ -z "$SRC" ]]; then
  echo "error: cannot find pynescript package tree" >&2
  echo "set PYNESCRIPT_SRC=/path/to/pyne/src/pynescript" >&2
  echo "# (repo may be cloned as pyne or pynescript)" >&2
  exit 1
fi

echo "sync: $SRC  →  $DEST"
mkdir -p "$DEST"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.nbi' \
    --exclude='*.nbc' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    "$SRC/" "$DEST/"
else
  # Fallback without rsync
  rm -rf "$DEST"
  mkdir -p "$DEST"
  cp -a "$SRC/." "$DEST/"
  find "$DEST" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$DEST" \( -name '*.pyc' -o -name '*.nbi' -o -name '*.nbc' \) -delete 2>/dev/null || true
fi

# Refresh dist-info label from the synced package version
PKG_VER=""
if [[ -f "$DEST/__about__.py" ]]; then
  PKG_VER=$(python3 -c "import pathlib,re; t=pathlib.Path('$DEST/__about__.py').read_text(); m=re.search(r'__version__\\s*=\\s*[\"\\']([^\"\\']+)', t); print(m.group(1) if m else '')" 2>/dev/null || true)
fi
if [[ -n "$PKG_VER" ]]; then
  shopt -s nullglob
  for old in "$ROOT"/python_modules/pynescript-*.dist-info; do
    base=$(basename "$old")
    if [[ "$base" != "pynescript-${PKG_VER}.dist-info" ]]; then
      dest_info="$ROOT/python_modules/pynescript-${PKG_VER}.dist-info"
      if [[ ! -d "$dest_info" ]]; then
        mv "$old" "$dest_info"
      else
        rm -rf "$old"
      fi
    fi
  done
  shopt -u nullglob
  echo "ok: dist-info version $PKG_VER"
fi

# Sanity checks
need=(
  "$DEST/util/time_parts.py"
  "$DEST/compiler/engine.py"
  "$DEST/ast/helper.py"
  "$DEST/__init__.py"
)
for f in "${need[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "error: missing required file after sync: $f" >&2
    exit 1
  fi
done

PY_COUNT=$(find "$DEST" -name '*.py' | wc -l | tr -d ' ')
echo "ok: $PY_COUNT .py files under python_modules/pynescript"
echo "ok: util/time_parts.py present"
echo "next: npx wrangler deploy"
