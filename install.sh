#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_dir=${MELD_BUILD_DIR:-/var/tmp/meld.build}
prefix=${MELD_PREFIX:-/usr/local}
python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python_site_kind=$(python3 -c 'import sysconfig; print("dist-packages" if sysconfig.get_path("purelib").endswith("/dist-packages") else "site-packages")')
python_purelibdir="lib/python${python_version}/${python_site_kind}"
apt_packages=(
    meson
    ninja-build
    pkg-config
    gettext
    itstool
    libgtk-4-dev
    libglib2.0-dev
    libgtksourceview-5-dev
    libadwaita-1-dev
    python3-gi
    python3-gi-cairo
    python3-cairo
    python-gi-dev
    python3-cairo-dev
)

missing=()

for command_name in meson ninja pkg-config msgfmt glib-compile-schemas itstool sudo; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        missing+=("command: $command_name")
    fi
done

pkg_config_requirements=(
    "gtk4 >= 4.12"
    "glib-2.0 >= 2.80"
    "gtksourceview-5 >= 5.0"
    "libadwaita-1 >= 1.6"
    "pygobject-3.0 >= 3.46"
    "py3cairo >= 1.15.0"
)
for requirement in "${pkg_config_requirements[@]}"; do
    if ! pkg-config --exists "$requirement"; then
        missing+=("pkg-config: $requirement")
    fi
done

if ! python3 -c 'import gi, cairo' >/dev/null 2>&1; then
    missing+=("Python modules: gi and cairo")
fi

if ((${#missing[@]})); then
    if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        printf 'Installing missing Meld prerequisites with apt...\n' >&2
        sudo apt-get update
        sudo apt-get install -y "${apt_packages[@]}"
        exec "$0" "$@"
    fi

    printf 'Meld build prerequisites are missing:\n' >&2
    printf '  - %s\n' "${missing[@]}" >&2
    printf '\nOn Debian/Ubuntu, install the usual prerequisites with:\n' >&2
    printf '  sudo apt install %s\n' "${apt_packages[*]}" >&2
    printf '\nAutomatic installation requires apt-get and sudo.\n' >&2
    exit 1
fi

meson setup "$build_dir" "$script_dir" \
    --prefix="$prefix" \
    -Dpython.purelibdir="$python_purelibdir" \
    --buildtype=release
meson compile -C "$build_dir"
sudo meson install -C "$build_dir"

printf '\nInstalled Meld under %s\n' "$prefix"
printf 'Run: %s/bin/meld --version\n' "$prefix"
