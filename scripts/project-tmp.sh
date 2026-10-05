#!/usr/bin/env bash
# Portable project-owned scratch resolver. Resolve before exporting child TMPDIR.
set -euo pipefail

project=calibre-umcp
if [[ -z "${PROJECT_ORIGINAL_TMPDIR+x}" ]]; then
  export PROJECT_ORIGINAL_TMPDIR="${TMPDIR:-}"
fi

is_ci() {
  case "${CI:-}" in ''|0|false|FALSE) ;; *) return 0;; esac
  case "${GITHUB_ACTIONS:-}:${GITLAB_CI:-}:${TF_BUILD:-}:${CIRCLECI:-}" in
    *true*|*True*|*TRUE*) return 0;;
  esac
  return 1
}

path_usable() {
  local path="$1" parent ancestor
  [[ "$path" == /* ]] || return 1
  case "/${path#/}/" in */../*|*/./*) return 1;; esac
  [[ ! -L "$path" ]] || return 1
  ancestor="${path%/*}"
  while [[ -n "$ancestor" && "$ancestor" != / ]]; do
    [[ ! -L "$ancestor" || "$ancestor" == /workspace ]] || return 1
    ancestor="${ancestor%/*}"
  done
  if [[ -e "$path" ]]; then
    [[ -d "$path" && -O "$path" && -w "$path" && -x "$path" ]] || return 1
  fi
  parent="$path"
  while [[ ! -e "$parent" && ! -L "$parent" ]]; do parent="${parent%/*}"; [[ -n "$parent" ]] || parent=/; done
  [[ -d "$parent" && -w "$parent" && -x "$parent" ]]
}

resolve_root() {
  local base candidate explicit_base_root='' bases=()
  if [[ -n "${PROJECT_TMP_BASE+x}" ]]; then
    [[ -n "$PROJECT_TMP_BASE" ]] || { echo 'PROJECT_TMP_BASE must not be empty' >&2; return 1; }
    explicit_base_root="${PROJECT_TMP_BASE%/}/$project"
    path_usable "$explicit_base_root" || { echo 'PROJECT_TMP_BASE must be a usable absolute base' >&2; return 1; }
  fi
  if [[ -n "${PROJECT_TMP_ROOT+x}" ]]; then
    candidate="${PROJECT_TMP_ROOT%/}"
    [[ "${candidate##*/}" == "$project" ]] && path_usable "$candidate" || {
      echo 'PROJECT_TMP_ROOT must be a usable absolute non-symlink path ending in calibre-umcp' >&2; return 1;
    }
    [[ -z "$explicit_base_root" || "$candidate" == "$explicit_base_root" ]] || {
      echo 'Conflicting PROJECT_TMP_BASE and PROJECT_TMP_ROOT' >&2; return 1;
    }
    printf '%s\n' "$candidate"; return
  fi
  if [[ -n "$explicit_base_root" ]]; then printf '%s\n' "$explicit_base_root"; return; fi
  if is_ci; then
    bases=("${RUNNER_TEMP:-}" "$PROJECT_ORIGINAL_TMPDIR" /tmp)
  else
    bases=(/workspace/tmp /tmp)
  fi
  for base in "${bases[@]}"; do
    [[ -n "$base" ]] || continue
    candidate="${base%/}/$project"
    if path_usable "$candidate"; then printf '%s\n' "$candidate"; return; fi
  done
  echo 'No writable project-owned temporary root available' >&2
  return 1
}

root="$(resolve_root)"
case "${1:-root}" in
  root) printf '%s\n' "$root" ;;
  paths) printf '%s\n' "PROJECT_TMP_ROOT=$root" "CACHE_ROOT=$root/cache" "BUILD_ROOT=$root/build" "TEST_ROOT=$root/tests" "LOG_ROOT=$root/logs" "RUN_ROOT=$root/runs" ;;
  init)
    for path in "$root" "$root/cache" "$root/build" "$root/tests" "$root/logs" "$root/runs"; do
      path_usable "$path" || { echo "Unsafe scratch path: $path" >&2; exit 1; }
    done
    mkdir -p "$root/cache" "$root/build" "$root/tests" "$root/logs" "$root/runs"
    ;;
  *) echo 'Usage: project-tmp.sh root|paths|init' >&2; exit 1 ;;
esac
