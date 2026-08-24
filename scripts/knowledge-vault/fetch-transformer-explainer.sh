#!/usr/bin/env bash
set -euo pipefail

VAULT_DIR="${VAULT_DIR:?set by 'woon knowledge vault-tool fetch-transformer-explainer'}"
CACHE_DIR="${TRANSFORMER_EXPLAINER_CACHE_DIR:-${VAULT_DIR}/sources/.cache/transformer-explainer}"

REPO_COMMIT="bfe50afba10b9b560b84143ee1107d977defa74f"
SOURCE_ARCHIVE_URL="https://github.com/poloclub/transformer-explainer/archive/${REPO_COMMIT}.tar.gz"
TOKENIZER_REVISION="bf2c7f02e0b826c60d03af341171bde20893da66" # gitleaks:allow -- pinned public Git revision
TOKENIZER_BASE_URL="https://huggingface.co/Xenova/gpt2/resolve/${TOKENIZER_REVISION}"
TOKENIZER_JSON_SHA256="cda20b8ca044949aa07ac4078420c80d1a57139d5f9f33700e46fb2d891e7c66" # gitleaks:allow -- public artifact checksum
TOKENIZER_CONFIG_SHA256="551e26ec611d8d0c8edc3ef72e518a38418cb71f40de1347dd486a595e1557d7" # gitleaks:allow -- public artifact checksum

usage() {
  cat <<'EOF'
Usage: fetch-transformer-explainer.sh

Create the pinned Transformer Explainer source cache inside the Vault.
Override the destination with TRANSFORMER_EXPLAINER_CACHE_DIR.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 0 ]]; then
  usage >&2
  exit 2
fi

STAMP_FILE="${CACHE_DIR}/.source-commit"
if [[ -f "${STAMP_FILE}" ]] && [[ "$(<"${STAMP_FILE}")" == "${REPO_COMMIT}" ]]; then
  echo "Transformer Explainer cache already matches ${REPO_COMMIT}: ${CACHE_DIR}"
  exit 0
fi

if [[ -e "${CACHE_DIR}" ]]; then
  echo "Refusing to overwrite an existing cache with an unknown state: ${CACHE_DIR}" >&2
  echo "Move or remove that exact directory, then run this script again." >&2
  exit 1
fi

CACHE_PARENT="$(dirname "${CACHE_DIR}")"
mkdir -p "${CACHE_PARENT}"
STAGING_DIR="$(mktemp -d "${CACHE_PARENT}/.transformer-explainer.XXXXXX")"

cleanup() {
  rm -rf -- "${STAGING_DIR}"
}
trap cleanup EXIT

ARCHIVE_FILE="${STAGING_DIR}/source.tar.gz"
SOURCE_DIR="${STAGING_DIR}/source"
mkdir "${SOURCE_DIR}"

echo "Downloading Transformer Explainer ${REPO_COMMIT}..."
curl --fail --location --retry 3 --output "${ARCHIVE_FILE}" "${SOURCE_ARCHIVE_URL}"
tar -xzf "${ARCHIVE_FILE}" -C "${SOURCE_DIR}" --strip-components=1

TOKENIZER_DIR="${SOURCE_DIR}/static/models/Xenova/gpt2"
mkdir -p "${TOKENIZER_DIR}"
curl --fail --location --retry 3 \
  --output "${TOKENIZER_DIR}/tokenizer.json" \
  "${TOKENIZER_BASE_URL}/tokenizer.json"
curl --fail --location --retry 3 \
  --output "${TOKENIZER_DIR}/tokenizer_config.json" \
  "${TOKENIZER_BASE_URL}/tokenizer_config.json"

verify_sha256() {
  local expected="$1"
  local source_file="$2"
  local actual
  actual="$(shasum -a 256 "${source_file}" | awk '{print $1}')"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "SHA-256 mismatch: ${source_file}" >&2
    echo "expected ${expected}" >&2
    echo "actual   ${actual}" >&2
    exit 1
  fi
}

verify_sha256 "${TOKENIZER_JSON_SHA256}" "${TOKENIZER_DIR}/tokenizer.json"
verify_sha256 "${TOKENIZER_CONFIG_SHA256}" "${TOKENIZER_DIR}/tokenizer_config.json"

printf '%s\n' "${REPO_COMMIT}" > "${SOURCE_DIR}/.source-commit"
mv "${SOURCE_DIR}" "${CACHE_DIR}"

echo "Prepared Transformer Explainer cache: ${CACHE_DIR}"
echo "Next: cd \"${CACHE_DIR}\" and install the documented Svelte 4 dependency set."
