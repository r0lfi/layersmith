# shellcheck shell=bash
# Shared by the OpenShift Admin Toolkit commands (oc-status, oc-gpu, ...).
#
# Exit codes every command uses:
#   0 ok   1 problems found   2 no context / not logged in   3 cluster unreachable or TLS failure
#   4 not allowed (RBAC)   64 usage error   127 a required CLI is missing
#
# Nothing here prints a token, a password or certificate data: kubeconfigs are
# read through `oc config view`, which redacts them unless --raw is given, and
# --raw is never used.

OA_TIMEOUT=${OPENSHIFT_ADMIN_TIMEOUT:-15s}
# shellcheck disable=SC2034  # used by the commands that source this file
OA_SHARE=/usr/local/share/openshift-admin
if [ -r /etc/openshift-admin/toolkit.env ]; then
  # shellcheck source=/dev/null
  . /etc/openshift-admin/toolkit.env
fi
OA_PROD_PATTERN=${OPENSHIFT_ADMIN_PROD_PATTERN:-prod}

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  OA_RED=$'\e[31m' OA_GREEN=$'\e[32m' OA_YELLOW=$'\e[33m' OA_BOLD=$'\e[1m' OA_PROD=$'\e[1;97;41m' OA_OFF=$'\e[0m'
else
  OA_RED='' OA_GREEN='' OA_YELLOW='' OA_BOLD='' OA_PROD='' OA_OFF=''
fi

oa_die() {  # oa_die EXIT_CODE MESSAGE...
  local code=$1
  shift
  printf '%s\n' "${OA_RED}$*${OA_OFF}" >&2
  exit "$code"
}

oa_warn() { printf '%s\n' "${OA_YELLOW}WARNING:${OA_OFF} $*" >&2; }
oa_ok() { printf '%s\n' "${OA_GREEN}$*${OA_OFF}"; }
oa_head() { printf '\n%s\n' "${OA_BOLD}== $* ==${OA_OFF}"; }

# oa_usage TEXT "$@": print TEXT and exit 0 for -h/--help.
oa_usage() {
  local text=$1
  shift
  case "${1:-}" in -h | --help) printf '%s\n' "$text"; exit 0 ;; esac
}

oa_need() {
  local cli
  for cli in "$@"; do
    command -v "$cli" >/dev/null 2>&1 || oa_die 127 "$cli is not installed in this image"
  done
}

# oc with a request timeout, so an unreachable cluster fails fast instead of hanging.
oc_() { oc --request-timeout="$OA_TIMEOUT" "$@"; }

oa_context() { oc config current-context 2>/dev/null; }
oa_server() { oc config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null; }
oa_namespace() {
  local ns
  ns=$(oc config view --minify -o jsonpath='{..namespace}' 2>/dev/null)
  printf '%s\n' "${ns:-default}"
}

# oa_is_prod [CONTEXT [SERVER]]: true when the context name or API server matches the production pattern.
oa_is_prod() {
  local ctx=${1-$(oa_context)} server=${2-$(oa_server)}
  [ -n "$ctx$server" ] || return 1
  [[ ${ctx,,} =~ ${OA_PROD_PATTERN,,} || ${server,,} =~ ${OA_PROD_PATTERN,,} ]]
}

oa_prod_banner() {
  if oa_is_prod; then
    printf '%s\n' "${OA_PROD} PRODUCTION ${OA_OFF} ${OA_BOLD}$(oa_context)${OA_OFF} ($(oa_server)) - changes here affect production" >&2
  fi
}

# oa_require_login: sets OA_CONTEXT and OA_USER, or exits 2/3 with an explanation.
oa_require_login() {
  oa_need oc
  OA_CONTEXT=$(oa_context)
  if [ -z "$OA_CONTEXT" ]; then
    {
      echo "No current Kubernetes context: you are not logged in to a cluster."
      echo "  Log in:            oc login https://api.<cluster>.<domain>:6443"
      echo "  Or pick a context: oc-context"
      [ -n "${KUBECONFIG:-}" ] && echo "  KUBECONFIG:        $KUBECONFIG"
    } >&2
    exit 2
  fi
  local out
  if out=$(oc_ whoami 2>&1); then
    # shellcheck disable=SC2034  # used by the commands that source this file
    OA_USER=$out
    return 0
  fi
  local server
  server=$(oa_server)
  case $out in
    *x509* | *certificate*)
      oa_die 3 "TLS verification failed for $server. Add the cluster's CA (openshift-admin-help ca); the toolkit never turns TLS verification off." ;;
    *"connection refused"* | *"was refused"* | *"no such host"* | *timeout* | *Timeout* | *"no route to host"* | *unreachable* | *EOF*)
      oa_die 3 "Cannot reach the API server of context '$OA_CONTEXT' ($server): ${out##*: }" ;;
    *Unauthorized* | *"must be logged in"* | *credentials* | *expired* | *Forbidden*)
      oa_die 2 "Not logged in to '$OA_CONTEXT' or the session expired. Run: oc login $server" ;;
    *)
      oa_die 2 "oc whoami failed for '$OA_CONTEXT': $out" ;;
  esac
}

# oa_can VERB RESOURCE [ARGS...]: RBAC check that never prints anything.
oa_can() { oc_ auth can-i "$@" >/dev/null 2>&1; }

# oa_has_api GROUP: true when the cluster serves that API group (CRD installed).
oa_has_api() { oc_ api-resources --api-group="$1" -o name 2>/dev/null | grep -q .; }

# oa_scope "$@": "-A" when the user may list cluster-wide and gave no -n, else "-n NS".
oa_scope_args() {
  local ns=$1 all=$2 resource=$3
  if [ -n "$ns" ]; then
    printf '%s\n' "-n" "$ns"
  elif [ "$all" = 1 ] || oa_can list "$resource" --all-namespaces; then
    printf '%s\n' "-A"
  else
    printf '%s\n' "-n" "$(oa_namespace)"
  fi
}

# oa_confirm QUESTION: yes only from an interactive "y"; non-interactive runs say no.
oa_confirm() {
  local answer
  [ -t 0 ] || return 1
  read -r -p "$1 [y/N] " answer
  [[ $answer =~ ^[Yy]([Ee][Ss])?$ ]]
}

# oa_table: align tab-separated stdin into columns.
oa_table() { column -t -s $'\t' 2>/dev/null || cat; }
