#!/usr/bin/env bash
# Portable project-owned scratch resolver. Resolve before exporting TMPDIR.
set -euo pipefail

project=calibre-umcp

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
  local base candidate
  if [[ -n "${PROJECT_TMP_ROOT+x}" ]]; then
    candidate="${PROJECT_TMP_ROOT%/}"
    [[ "${candidate##*/}" == "$project" ]] && path_usable "$candidate" || {
      echo 'PROJECT_TMP_ROOT must be a usable absolute non-symlink path ending in calibre-umcp' >&2
      return 1
    }
    printf '%s\n' "$candidate"
    return
  fi
  for base in /workspace/tmp "${RUNNER_TEMP:-}" "${TMPDIR:-}" /tmp; do
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
  paths) printf '%s\n' "PROJECT_TMP_ROOT=$root" "CACHE_ROOT=$root/cache" "BUILD_ROOT=$root/build" "RUN_ROOT=$root/runs" ;;
  init)
    for path in "$root" "$root/cache" "$root/build" "$root/runs"; do
      path_usable "$path" || { echo "Unsafe scratch path: $path" >&2; exit 1; }
    done
    mkdir -p "$root/cache" "$root/build" "$root/runs"
    ;;
  *) echo 'Usage: project-tmp.sh root|paths|init' >&2; exit 1 ;;
esac
