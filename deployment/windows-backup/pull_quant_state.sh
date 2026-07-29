#!/usr/bin/env bash
# Run inside WSL on Windows. It streams a complete custom pg_dump to Windows.
set -euo pipefail

usage() {
  echo "usage: $0 <quant-backup-pull@server> <destination-dir> <private-key>" >&2
  exit 64
}

[[ "$#" -eq 3 ]] || usage
readonly remote="$1"
readonly destination="$2"
readonly identity_file="$3"
readonly retain_count="${RETAIN_COUNT:-7}"
readonly minimum_free_gib="${MINIMUM_FREE_GIB:-20}"

command -v ssh >/dev/null
command -v pg_restore >/dev/null
command -v sha256sum >/dev/null
[[ -r "${identity_file}" ]] || { echo "private key is not readable" >&2; exit 66; }
mkdir -p "${destination}"

available_bytes="$(df -PB1 "${destination}" | awk 'NR==2 {print $4}')"
required_bytes=$((minimum_free_gib * 1024 * 1024 * 1024))
if (( available_bytes < required_bytes )); then
  echo "need at least ${minimum_free_gib}GiB free at ${destination}" >&2
  exit 75
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
final_path="${destination}/quant_audit-${timestamp}.dump"
partial_path="${final_path}.partial"
trap 'rm -f "${partial_path}"' EXIT

ssh -i "${identity_file}" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  "${remote}" dump > "${partial_path}"
pg_restore --list "${partial_path}" >/dev/null

byte_count="$(stat -c '%s' "${partial_path}")"
sha256="$(sha256sum "${partial_path}" | awk '{print $1}')"
completed_at="$(date +%s)"
mv -- "${partial_path}" "${final_path}"
trap - EXIT

ssh -i "${identity_file}" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  "${remote}" receipt "${completed_at}" "${byte_count}" "${sha256}"

mapfile -t expired < <(
  find "${destination}" -maxdepth 1 -type f -name 'quant_audit-*.dump' -printf '%T@ %p\n' \
    | sort -nr \
    | awk -v keep="${retain_count}" 'NR > keep {sub(/^[^ ]+ /, ""); print}'
)
for path in "${expired[@]:-}"; do
  [[ -n "${path}" ]] && rm -f -- "${path}"
done

echo "backup_complete path=${final_path} bytes=${byte_count} sha256=${sha256}"
