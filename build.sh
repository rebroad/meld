#!/usr/bin/env bash
set -Eeuo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_dir=${MELD_BUILD_DIR:-/mnt/kingston/builds/rebroad/src/meld.build/_build-from-source}
python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python_site_kind=$(python3 -c 'import sysconfig; print("dist-packages" if sysconfig.get_path("purelib").endswith("/dist-packages") else "site-packages")')
python_purelibdir="lib/python${python_version}/${python_site_kind}"
meson_python_dir=(-Dpython.purelibdir="$python_purelibdir")

if [[ -f "$build_dir/meson-private/coredata.dat" ]]; then
    meson setup --reconfigure "$build_dir" "$source_dir" "${meson_python_dir[@]}"
else
    meson setup "$build_dir" "$source_dir" "${meson_python_dir[@]}"
fi

meson compile -C "$build_dir"

printf '\nBuild complete: %s\n' "$build_dir"
printf 'Source commit: '
git -C "$source_dir" rev-parse --short=10 HEAD
