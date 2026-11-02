#!/bin/bash
# Last build step: locale, a writable passwd/group for ensure-user, shell
# completion and the version inventory.
set -euo pipefail

locale=${1:?usage: finish.sh LOCALE}
share=/usr/local/share/opencode

echo "LANG=$locale" >/etc/locale.conf
locale -a | grep -qix "${locale/UTF-8/utf8}" || { echo "ERROR: locale $locale is not installed" >&2; exit 1; }

# No privilege inside: users only ever run as themselves here.
if rpm -q sudo >/dev/null 2>&1; then
  dnf -y remove sudo && dnf clean all
fi

# Generators may write config or cache files; none of that belongs in the image.
scratch=$(mktemp -d)
export HOME=$scratch XDG_CONFIG_HOME=$scratch XDG_CACHE_HOME=$scratch XDG_DATA_HOME=$scratch XDG_STATE_HOME=$scratch
mkdir -p /usr/share/bash-completion/completions
opencode completion >/usr/share/bash-completion/completions/opencode 2>/dev/null \
  || echo "note: opencode printed no bash completion"

/usr/local/bin/opencode-versions --json >"$share/versions.json" || {
  echo "ERROR: a tool is missing or reports an unexpected version:" >&2
  jq -r '.[] | select(.status != "ok") | "  \(.tool) \(.binary): \(.status) (\(.version); expected \(.expected))"' "$share/versions.json" >&2
  exit 1
}
jq -r '.[] | "  \(.binary)\t\(.version)"' "$share/versions.json" | column -t -s $'\t'

rm -rf "$scratch" /root/.cache /root/.config /root/.local /tmp/* /var/tmp/*
