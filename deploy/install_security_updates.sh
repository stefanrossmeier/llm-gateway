#!/bin/sh
# Reviewed Debian security artifact. No floating package upgrade or new dependency.
set -eu
gateway_zlib_version='1:1.3.dfsg+really1.3.1-1+deb13u1'
case "$(dpkg --print-architecture)" in
    arm64) gateway_zlib_sha='0e41b3d7ad8ad7fcd36dd4d61ca91a70018478fc4af652e044f4be770784ddff' ;;
    amd64) gateway_zlib_sha='8c27c342230503b73dfde629642c3b352e87b554e06d60cf58772f236a305ceb' ;;
    *) echo 'Unreviewed container architecture' >&2; exit 1 ;;
esac
gateway_zlib_work=$(mktemp -d)
trap 'rm -rf "$gateway_zlib_work" /var/lib/apt/lists/*' EXIT
chown _apt:root "$gateway_zlib_work"
apt-get update -qq
cd "$gateway_zlib_work"
apt-get download "zlib1g=$gateway_zlib_version"
set -- ./zlib1g_*.deb
test "$#" = 1
printf '%s  %s\n' "$gateway_zlib_sha" "$1" | sha256sum --check
dpkg-deb --control "$1" control
for gateway_control in preinst postinst prerm postrm config; do
    test ! -e "control/$gateway_control"
done
# Only the already-installed system ldconfig trigger is permitted.
test "$(sed '/^#/d;/^[[:space:]]*$/d' control/triggers)" = 'activate-noawait ldconfig'
dpkg --install "$1"
