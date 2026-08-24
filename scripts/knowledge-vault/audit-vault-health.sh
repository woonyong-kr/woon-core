#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VAULT_ROOT="${1:-$PWD}"

if [[ ! -d "${VAULT_ROOT}" ]]; then
  echo "vault path is not a directory: ${VAULT_ROOT}" >&2
  exit 2
fi

(
  cd "${VAULT_ROOT}"
  uv run --project "${CORE_ROOT}" python "${CORE_ROOT}/src/woon_core/knowledge/vault_tools/audit-vault-health.py"
  uv run --project "${CORE_ROOT}" python "${CORE_ROOT}/src/woon_core/knowledge/vault_tools/audit-folder-depth.py"
)
