#!/bin/bash
# Last build step: time zone, locale, bash completion and the version inventory.
set -euo pipefail

timezone=${1:?usage: finish.sh TIMEZONE LOCALE}
locale=${2:?usage: finish.sh TIMEZONE LOCALE}
share=/usr/local/share/openshift-admin
completions=/usr/share/bash-completion/completions

[[ -e /usr/share/zoneinfo/$timezone ]] || { echo "ERROR: unknown time zone $timezone" >&2; exit 1; }
ln -sf "/usr/share/zoneinfo/$timezone" /etc/localtime
echo "$timezone" >/etc/timezone
echo "LANG=$locale" >/etc/locale.conf
locale -a | grep -qix "${locale/UTF-8/utf8}" || { echo "ERROR: locale $locale is not installed" >&2; exit 1; }

# Completion generators may write config or cache files; none of that belongs in the image.
scratch=$(mktemp -d)
export HOME=$scratch XDG_CONFIG_HOME=$scratch XDG_CACHE_HOME=$scratch
mkdir -p "$completions"
for tool in oc kubectl helm kustomize k9s stern opm operator-sdk yq; do
  command -v "$tool" >/dev/null 2>&1 || continue
  case $tool in
    yq) yq shell-completion bash >"$completions/yq" ;;
    *) "$tool" completion bash >"$completions/$tool" ;;
  esac
  [[ -s $completions/$tool ]] || { echo "ERROR: $tool produced no bash completion" >&2; exit 1; }
  echo "bash completion: $tool"
done
for helper in oc-context oc-debug-node kubectx kubens oc-gpu oc-ai oc-storage oc-routes oc-logs oc-prom \
  openshift-admin-help openshift-admin-versions; do
  ln -sf openshift-admin "$completions/$helper"
done

/usr/local/bin/openshift-admin-versions --json >"$share/versions.json" || {
  echo "ERROR: a tool is missing or reports an unexpected version:" >&2
  jq -r '.[] | select(.status != "ok") | "  \(.tool) \(.binary): \(.status) (\(.version); expected \(.expected))"' "$share/versions.json" >&2
  exit 1
}
jq -r '.[] | "  \(.binary)\t\(.version)"' "$share/versions.json" | column -t -s $'\t'

rm -rf "$scratch" /root/.kube /root/.cache /root/.config /tmp/* /var/tmp/*
