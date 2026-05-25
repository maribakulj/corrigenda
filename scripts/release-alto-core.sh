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
# Optional ``(?::\s*[^=]+)?`` clause matches both the current
#   __version__ = \"X.Y.Z\"
# and a future type-annotated form
#   __version__: Final[str] = \"X.Y.Z\"
# so adding a type hint cannot silently make the release script abort.
m = re.search(r'__version__\s*(?::\s*[^=]+)?=\s*[\"\\']([^\"\\']+)[\"\\']', init)
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

# Smoke-install the wheel into an isolated venv and exercise the
# public API. _smoke_imports.py is the single source of truth shared
# with .github/workflows/{ci,publish-alto-core}.yml — see roadmap
# L5 / B6. A two-step run keeps the version-equality check here in
# bash (the smoke script itself stays consumer-portable, it doesn't
# know about the release variable VERSION).
SMOKE_VENV="$(mktemp -d)/venv"
python -m venv "${SMOKE_VENV}"
"${SMOKE_VENV}/bin/pip" install --quiet dist/*.whl
"${SMOKE_VENV}/bin/python" _smoke_imports.py
INSTALLED_VERSION=$("${SMOKE_VENV}/bin/python" -c "import alto_core; print(alto_core.__version__)")
if [[ "${INSTALLED_VERSION}" != "${VERSION}" ]]; then
    echo "ERROR: built wheel reports version ${INSTALLED_VERSION}, expected ${VERSION}" >&2
    exit 1
fi
rm -rf "${SMOKE_VENV%/venv}"

if [[ -z "${TARGET}" ]]; then
    echo "==> No upload flag — stopping after smoke. Pass --testpypi or --pypi to publish."
    exit 0
fi

# Roadmap L7 / P7 — anti-double-upload guard.
# Hit the index's JSON metadata endpoint and bail if VERSION is already
# present. Without this, twine surfaces the duplicate as an opaque 403
# AFTER the upload attempt, which (a) wastes a build slot and (b) leaves
# the operator guessing whether they're racing with another release.
if [[ "${TARGET}" == "testpypi" ]]; then
    INDEX_JSON_URL="https://test.pypi.org/pypi/alto-core/json"
else
    INDEX_JSON_URL="https://pypi.org/pypi/alto-core/json"
fi
INDEX_TMP="$(mktemp)"
# `-sS` (silent + show errors), NOT `-f` — we want curl to write the
# response body + populate `${http_code}` even on 4xx, so we can branch
# on 404 (first release) vs 200 (duplicate check). `-f` would exit
# non-zero on 4xx and the `|| echo "000"` would concatenate to the
# http_code already emitted.
HTTP_CODE=$(curl -sS -o "${INDEX_TMP}" -w "%{http_code}" "${INDEX_JSON_URL}" 2>/dev/null || echo "000")
case "${HTTP_CODE}" in
    200)
        if python -c "
import json, sys
data = json.load(open('${INDEX_TMP}'))
sys.exit(0 if '${VERSION}' in data.get('releases', {}) else 1)
"; then
            echo "ERROR: alto-core ${VERSION} is already on ${TARGET}." >&2
            echo "       Bump src/alto_core/__init__.py::__version__ and add a CHANGELOG entry." >&2
            rm -f "${INDEX_TMP}"
            exit 1
        fi
        echo "==> Index check: ${VERSION} not yet on ${TARGET}, safe to upload."
        ;;
    404)
        echo "==> Index check: alto-core not yet published on ${TARGET}, first release."
        ;;
    000)
        echo "WARNING: index check failed (network?); proceeding without guarantee." >&2
        ;;
    *)
        echo "WARNING: index check returned HTTP ${HTTP_CODE}; proceeding without guarantee." >&2
        ;;
esac
rm -f "${INDEX_TMP}"

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
