# OpenShift Admin Toolkit shell setup. Sourced by login shells (/etc/profile)
# and, on Enterprise Linux, by interactive shells through /etc/bashrc.
#
#   OPENSHIFT_ADMIN_KUBECONFIG_AUTOLOAD=0   do not build KUBECONFIG from ~/.kube/clusters
#   OPENSHIFT_ADMIN_PROMPT=0                no context/namespace in the prompt
#   OPENSHIFT_ADMIN_PROD_PATTERN=...        what marks a production context (extended regex)

[ -n "${BASH_VERSION:-}" ] || return 0

if [ -r /etc/openshift-admin/toolkit.env ]; then
  . /etc/openshift-admin/toolkit.env
fi

# Several clusters: ~/.kube/config first (oc login and context switches write
# there), then one file per cluster from ~/.kube/clusters.
if [ -z "${KUBECONFIG:-}" ] && [ "${OPENSHIFT_ADMIN_KUBECONFIG_AUTOLOAD:-1}" = 1 ] && [ -d "$HOME/.kube/clusters" ]; then
  __oa_files=$(find -L "$HOME/.kube/clusters" -maxdepth 1 -type f \
    \( -name '*.yaml' -o -name '*.yml' -o -name '*.kubeconfig' \) 2>/dev/null | LC_ALL=C sort | paste -sd: -)
  if [ -n "$__oa_files" ]; then
    export KUBECONFIG="$HOME/.kube/config:$__oa_files"
    # Context switches are written to the first file; without it kubectl would
    # write them into one of the per-cluster files instead.
    [ -e "$HOME/.kube/config" ] || (umask 077 && : >"$HOME/.kube/config")
  fi
  unset __oa_files
fi

case $- in *i*) ;; *) return 0 ;; esac

# Keep credentials out of the shell history.
HISTIGNORE="${HISTIGNORE:+$HISTIGNORE:}*--token*:*--password*:oc login*-p *:oc login*-p=*:*client-key-data*"

alias k='kubectl'
alias ocnodes='oc get nodes -o wide'
alias ocpods='oc get pods -A'
alias ochealth='oc get clusteroperators'
alias ocmcp='oc get mcp'
alias ocgpu='oc get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu'
if declare -F __start_kubectl >/dev/null 2>&1 || [ -r /usr/share/bash-completion/completions/kubectl ]; then
  complete -o default -F __start_kubectl k 2>/dev/null || true
fi

# Context, namespace and a PRODUCTION marker in the prompt. Recomputed only
# when KUBECONFIG or one of its files changes, so the prompt stays fast.
__oa_kube_state() {
  local files=${KUBECONFIG:-$HOME/.kube/config} f
  local key=$files
  local IFS=:
  for f in $files; do
    [ -e "$f" ] && key+=":$(stat -c %Y "$f" 2>/dev/null)"
  done
  [ "$key" = "${__OA_KEY:-}" ] && return
  __OA_KEY=$key
  __OA_CTX=$(kubectl config current-context 2>/dev/null)
  __OA_NS='' __OA_SERVER='' __OA_IS_PROD=0
  [ -n "$__OA_CTX" ] || return
  __OA_NS=$(kubectl config view --minify -o 'jsonpath={..namespace}' 2>/dev/null)
  __OA_SERVER=$(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}' 2>/dev/null)
  local pattern=${OPENSHIFT_ADMIN_PROD_PATTERN:-prod}
  if [[ ${__OA_CTX,,} =~ ${pattern,,} || ${__OA_SERVER,,} =~ ${pattern,,} ]]; then
    __OA_IS_PROD=1
  fi
}

__oa_prompt() {
  local rc=$?
  [ "${OPENSHIFT_ADMIN_PROMPT:-1}" = 1 ] || return $rc
  # A PS1 set after this profile (e.g. in ~/.bashrc) becomes the base we prefix.
  [ "$PS1" = "${__OA_LAST_PS1:-}" ] || __OA_BASE_PS1=$PS1
  __oa_kube_state
  # Names come from kubeconfig files, possibly someone else's: PS1 expands $(...)
  # and backticks, so only plain name characters may reach it.
  local segment='' name=${__OA_CTX//[^A-Za-z0-9._:\/@-]/_} ns=${__OA_NS//[^A-Za-z0-9._-]/_}
  if [ -n "$name" ]; then
    # oc login names contexts namespace/api-host:port/user; show the cluster and user.
    if [[ $name =~ ^[^/]*/([^/:]+)[^/]*/(.+)$ ]]; then
      name="${BASH_REMATCH[1]}(${BASH_REMATCH[2]})"
    fi
    if [ "$__OA_IS_PROD" = 1 ]; then
      segment="\[\e[1;97;41m\] PROD \[\e[0m\] \[\e[1;31m\][$name:${ns:-default}]\[\e[0m\] "
    else
      segment="\[\e[36m\][$name:${ns:-default}]\[\e[0m\] "
    fi
  fi
  PS1="$segment${__OA_BASE_PS1}"
  __OA_LAST_PS1=$PS1
  return $rc
}

if [[ ";${PROMPT_COMMAND:-};" != *";__oa_prompt;"* ]]; then
  PROMPT_COMMAND="__oa_prompt${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
fi

# Say it once per shell when the starting context is production.
__oa_kube_state
if [ "${__OA_IS_PROD:-0}" = 1 ] && [ -z "${__OA_WARNED:-}" ]; then
  printf '\e[1;97;41m PRODUCTION \e[0m current context %s (%s)\n' "$__OA_CTX" "$__OA_SERVER" >&2
  __OA_WARNED=1
fi
