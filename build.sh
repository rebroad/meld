#!/usr/bin/env bash
set -Eeuo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_dir=${MELD_BUILD_DIR:-/mnt/kingston/builds/rebroad/src/meld.build/_build-from-source}

if [[ -f "$build_dir/meson-private/coredata.dat" ]]; then
    meson setup --reconfigure "$build_dir" "$source_dir"
else
    meson setup "$build_dir" "$source_dir"
fi

meson compile -C "$build_dir"

printf '\nBuild complete: %s\n' "$build_dir"
printf 'Source commit: '
git -C "$source_dir" rev-parse --short=10 HEAD
