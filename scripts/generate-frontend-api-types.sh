#!/usr/bin/env bash
# Regenerate frontend/src/types/api.generated.ts from the backend's
# live OpenAPI spec.
#
# Usage:
#   scripts/generate-frontend-api-types.sh
#
# Requires the backend Python env (corrigenda editable + backend deps)
# to be set up locally. See CONTRIBUTING.md for the install recipe.
#
# Output:
#   - frontend/openapi.snapshot.json  : current OpenAPI spec, committed
#                                       so CI can diff future runs.
#   - frontend/src/types/api.generated.ts : TypeScript types, also
#                                           committed.
#
# Drift detection: a future CI job can re-run this script and `git
# diff --exit-code` the two files to catch backend/frontend type drift.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT="${REPO_ROOT}/frontend/openapi.snapshot.json"
TYPES="${REPO_ROOT}/frontend/src/types/api.generated.ts"

echo "==> Dumping OpenAPI spec from FastAPI app"
(cd "${REPO_ROOT}/backend" && python -c "
import json
from app.main import create_app
print(json.dumps(create_app().openapi(), indent=2))
") > "${SNAPSHOT}"
echo "    wrote ${SNAPSHOT} ($(wc -l < "${SNAPSHOT}") lines)"

echo "==> Generating TypeScript types"
(cd "${REPO_ROOT}/frontend" && npx --no openapi-typescript openapi.snapshot.json -o src/types/api.generated.ts)
echo "    wrote ${TYPES} ($(wc -l < "${TYPES}") lines)"

echo "==> Reformatting with Prettier"
(cd "${REPO_ROOT}/frontend" && npx --no prettier --write src/types/api.generated.ts openapi.snapshot.json)

echo "==> Done. Review the diff before committing:"
echo "    git diff -- frontend/openapi.snapshot.json frontend/src/types/api.generated.ts"
