#!/bin/sh
set -eu

version=4.5.12
archive="blender-${version}-linux-x64.tar.xz"
checksum=95e3a2dfedba3bd32ca54fc355eac6b15a11986954ccb02815a07535d0120a25
cache_root="${XDG_CACHE_HOME:-/tmp}/aizk-brand"
archive_path="$cache_root/$archive"
install_path="$cache_root/blender-$version"
blender="$install_path/blender"

if [ ! -x "$blender" ]; then
  mkdir -p "$cache_root"
  if [ ! -f "$archive_path" ]; then
    curl --fail --location --retry 3 \
      "https://download.blender.org/release/Blender4.5/$archive" \
      --output "$archive_path.part"
    mv "$archive_path.part" "$archive_path"
  fi
  actual=$(sha256sum "$archive_path" | cut -d ' ' -f 1)
  if [ "$actual" != "$checksum" ]; then
    echo "Blender archive checksum mismatch" >&2
    exit 1
  fi
  rm -rf "$install_path"
  mkdir -p "$install_path"
  tar -xJf "$archive_path" --strip-components 1 -C "$install_path"
fi

exec "$blender" "$@"
