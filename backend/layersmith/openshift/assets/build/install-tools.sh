#!/bin/bash
# Download, verify and install the toolkit's pinned clients. Runs once, at build time.
#
# Manifest lines (tab separated): URL  SHA256  MEMBER=DESTINATION...
# MEMBER is a file inside the .tar.gz, or "-" for a bare binary. A file whose
# sha256 differs from the pin fails the build: nothing unverified is installed,
# and nothing downloaded is ever executed before it is verified.
set -euo pipefail

manifest=${1:?usage: install-tools.sh MANIFEST}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

n=0
installed=()
while IFS=$'\t' read -r url sha targets; do
  [[ -z $url || $url == \#* ]] && continue
  n=$((n + 1))
  file="$work/download-$n"
  echo "--> $url"
  # HTTPS only, also after redirects (Red Hat's Helm downloads redirect to a CDN).
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 --retry-delay 5 \
    --connect-timeout 30 --max-time 1800 -o "$file" "$url"
  actual=$(sha256sum "$file" | cut -d' ' -f1)
  if [[ $actual != "$sha" ]]; then
    echo "ERROR: checksum mismatch for $url" >&2
    echo "  expected $sha" >&2
    echo "  got      $actual" >&2
    exit 1
  fi
  echo "    sha256 verified: $sha"
  for target in $targets; do
    member=${target%%=*}
    dest=${target#*=}
    if [[ $member == "-" ]]; then
      source=$file
    else
      mkdir -p "$work/x$n"
      tar -xzf "$file" -C "$work/x$n" --no-same-owner --no-same-permissions -- "$member"
      source="$work/x$n/$member"
    fi
    if [[ ! -f $source || -L $source ]]; then
      echo "ERROR: $member is not a regular file in $url" >&2
      exit 1
    fi
    install -D -m 0755 "$source" "$dest"
    installed+=("$dest")
    echo "    installed $dest"
  done
  rm -rf "$file" "$work/x$n"
done <"$manifest"

# Red Hat ships oc and kubectl as two identical binaries; one copy is enough.
# Only files this run installed are considered.
for oc in "${installed[@]}"; do
  [[ $oc == */oc ]] || continue
  for kubectl in "${installed[@]}"; do
    if [[ $kubectl == "${oc%/oc}/kubectl" ]] && cmp -s "$oc" "$kubectl"; then
      ln -f "$oc" "$kubectl"
      echo "$kubectl is a hard link to $oc (identical binaries)"
    fi
  done
done
echo "Installed $n verified downloads"
