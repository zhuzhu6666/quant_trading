#!/usr/bin/env bash
# Install the endpoint only. It does not install a Windows public key or a timer.
set -euo pipefail

readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly backup_user="quant-backup-pull"
readonly backup_home="/var/lib/${backup_user}"

[[ "${EUID}" -eq 0 ]] || { echo "run with sudo" >&2; exit 77; }
bash -n "${script_dir}/quant-state-backup-bridge"
bash -n "${script_dir}/quant-backup-ssh-gate"
visudo -cf "${script_dir}/quant-backup-pull.sudoers"

if ! id "${backup_user}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${backup_home}" --shell /bin/bash "${backup_user}"
fi
usermod --lock "${backup_user}"
install -d -o "${backup_user}" -g "${backup_user}" -m 0700 "${backup_home}/.ssh"
if [[ ! -e "${backup_home}/.ssh/authorized_keys" ]]; then
  install -o "${backup_user}" -g "${backup_user}" -m 0600 /dev/null "${backup_home}/.ssh/authorized_keys"
fi
install -o root -g root -m 0755 "${script_dir}/quant-state-backup-bridge" /usr/local/sbin/quant-state-backup-bridge
install -o root -g root -m 0755 "${script_dir}/quant-backup-ssh-gate" /usr/local/bin/quant-backup-ssh-gate
install -o root -g root -m 0440 "${script_dir}/quant-backup-pull.sudoers" /etc/sudoers.d/quant-backup-pull
visudo -cf /etc/sudoers.d/quant-backup-pull
echo "endpoint_ready user=${backup_user} authorized_key=absent timer=absent wal_archive=unchanged"
