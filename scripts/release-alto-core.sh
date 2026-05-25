#!/usr/bin/env bash
# Release script for packages/alto-core.
#
# Usage:
#   scripts/release-alto-core.sh             # build + smoke-install only
#   scripts/release-alto-core.sh --testpypi  # build + upload to TestPyPI
#   scripts/release-alto-core.sh --pypi      # build + upload to PyPI (prod)
#
# Pre-flight (manual):
#   1. Bump src/alto_core/__init__.py::__version__
#   2. Add a CHANGELOG.md entry under [Unreleased] → new version
#   3. Commit + tag (`git tag alto-core-vX.Y.Z`)
#   4. Run this script
#
# For TestPyPI/PyPI uploads, either:
#   - Configure ~/.pypirc with API tokens, OR
#   - export TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-xxx beforehand.
# Trusted Publishing (OIDC) is preferred — see
# .github/workflows/publish-alto-core.yml for the CI path.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="${REPO_ROOT}/packages/alto-core"
TARGET=""

if [[ "${1:-}" == "--testpypi" ]]; then
    TARGET="testpypi"
elif [[ "${1:-}" == "--pypi" ]]; then
    TARGET="pypi"
elif [[ "${1:-}" != "" ]]; then
    echo "Unknown flag: $1 (expected --testpypi or --pypi or no args)" >&2
    exit 2
fi

cd "${PKG_DIR}"

# Pre-flight: working tree must be clean (releases reproducible).
if [[ -n "$(git status --porcelain -- "${PKG_DIR}")" ]]; then
    echo "ERROR: ${PKG_DIR} has uncommitted changes; commit or stash first." >&2
    git status --short -- "${PKG_DIR}" >&2
    exit 1
fi

VERSION=$(python -c "
import sys, pathlib, re
init = pathlib.Path('src/alto_core/__init__.py').read_text()
m = re.search(r'__version__\s*=\s*[\"\\']([^\"\\']+)[\"\\']', init)
if not m:
    sys.exit('__version__ not found in src/alto_core/__init__.py')
print(m.group(1))
")
echo "==> Releasing alto-core ${VERSION}"

# Confirm the CHANGELOG mentions this version (cheap sanity).
if ! grep -q "## \[${VERSION}\]" CHANGELOG.md; then
    echo "ERROR: CHANGELOG.md has no entry for [${VERSION}]" >&2
    exit 1
fi

# Clean previous artefacts and build fresh.
rm -rf dist build
python -m build
echo "==> Built dist/:"
ls -lh dist/

# Smoke-install the wheel into an isolated venv and exercise the public API.
SMOKE_VENV="$(mktemp -d)/venv"
python -m venv "${SMOKE_VENV}"
"${SMOKE_VENV}/bin/pip" install --quiet dist/*.whl
"${SMOKE_VENV}/bin/python" - <<EOF
from alto_core import (
    CorrectionPipeline, BaseProvider, PipelineObserver, OutputWriter,
    parse_alto_file, build_document_manifest, rewrite_alto_file,
    DocumentManifest, LineManifest, HyphenRole, sanitize_error,
)
import alto_core
assert alto_core.__version__ == "${VERSION}"
print(f"smoke ok: alto-core {alto_core.__version__} installs + imports")
EOF
rm -rf "${SMOKE_VENV%/venv}"

if [[ -z "${TARGET}" ]]; then
    echo "==> No upload flag — stopping after smoke. Pass --testpypi or --pypi to publish."
    exit 0
fi

# Ensure twine is available.
python -m pip install --quiet --upgrade twine

if [[ "${TARGET}" == "testpypi" ]]; then
    echo "==> Uploading to TestPyPI"
    python -m twine upload --repository testpypi dist/*
    echo "==> Verify at https://test.pypi.org/project/alto-core/${VERSION}/"
else
    echo "==> Uploading to PyPI (production)"
    read -r -p "Confirm production upload of alto-core ${VERSION}? [y/N] " confirm
    if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
        echo "Aborted."
        exit 1
    fi
    python -m twine upload dist/*
    echo "==> Verify at https://pypi.org/project/alto-core/${VERSION}/"
fi
