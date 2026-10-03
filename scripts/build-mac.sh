#!/usr/bin/env bash
# Build EzGrader.app, plus a zip to share, on macOS:
#   scripts/build-mac.sh
# Output: dist/EzGrader.app and dist/EzGrader-<version>-macOS-<arch>.zip
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $PYTHON..."
  "$PYTHON" -m venv .venv
fi
echo "Installing build tools..."
.venv/bin/python -m pip install -q -r requirements.txt pyinstaller

echo "Building EzGrader.app..."
.venv/bin/pyinstaller EzGrader.spec --noconfirm --clean --log-level WARN

VERSION=$(.venv/bin/python -c "import ezgrader; print(ezgrader.__version__)")
ZIP="dist/EzGrader-$VERSION-macOS-$(uname -m).zip"
rm -f "$ZIP"
ditto -c -k --keepParent dist/EzGrader.app "$ZIP"  # ditto keeps the app bundle intact

echo
echo "Built dist/EzGrader.app"
echo "Zip to share: $ZIP"
