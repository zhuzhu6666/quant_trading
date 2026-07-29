#!/usr/bin/env bash
# Install one Windows public key as a forced, non-interactive backup-only key.
set -euo pipefail

readonly backup_user="quant-backup-pull"
readonly backup_home="/var/lib/${backup_user}"

[[ "${EUID}" -eq 0 ]] || { echo "run with sudo" >&2; exit 77; }
[[ "$#" -eq 1 && -f "$1" ]] || { echo "usage: sudo $0 /path/to/windows_backup.pub" >&2; exit 64; }

key="$(tr -d '\r\n' < "$1")"
[[ "${key}" =~ ^ssh-ed25519[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || {
  echo "only one ssh-ed25519 public key is accepted" >&2
  exit 64
}

temp="$(mktemp)"
trap 'rm -f "${temp}"' EXIT
printf 'restrict,command="/usr/local/bin/quant-backup-ssh-gate" %s\n' "${key}" > "${temp}"
install -o "${backup_user}" -g "${backup_user}" -m 0600 "${temp}" "${backup_home}/.ssh/authorized_keys"
echo "windows_backup_key_installed user=${backup_user}"
