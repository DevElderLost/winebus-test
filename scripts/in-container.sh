#!/usr/bin/env bash
# Dijalankan DI DALAM container arm64 (ubuntu/debian). Memasang dependensi lalu memanggil build-winebus.sh.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  build-essential git ca-certificates flex bison pkg-config \
  libudev-dev libsdl2-dev linux-libc-dev binutils file patch

exec bash "$(dirname "${BASH_SOURCE[0]}")/build-winebus.sh"
