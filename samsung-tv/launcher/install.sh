#!/bin/sh
# Build, sign and install Clean Home on the TV.
# Needs Tizen Studio with a Samsung TV certificate profile (see README.md).
# Usage: ./install.sh TV_IP          (uninstall: ./install.sh TV_IP --remove)
set -e
TV=${1:?usage: ./install.sh TV_IP [--remove]}
PROFILE=${PROFILE:-CleanHome}
TIZEN_HOME=${TIZEN_HOME:-$HOME/tizen-studio}
TIZEN=$TIZEN_HOME/tools/ide/bin/tizen
SDB=$TIZEN_HOME/tools/sdb
APP_ID=CleanHome1.CleanHome
HERE=$(cd "$(dirname "$0")" && pwd)

[ -x "$TIZEN" ] || { echo "Tizen Studio not found in $TIZEN_HOME (set TIZEN_HOME)"; exit 1; }

echo "== connecting to $TV"
"$SDB" connect "$TV"
DEVICE=$("$SDB" devices | awk -v tv="$TV" 'index($1, tv) == 1 {print $3}')
[ -n "$DEVICE" ] || { echo "TV not connected. Is Developer Mode on, with this computer's IP as Host PC IP?"; exit 1; }
echo "== TV: $DEVICE"

if [ "$2" = "--remove" ]; then
    "$TIZEN" uninstall -p "$APP_ID" -t "$DEVICE"
    exit 0
fi

BUILD=$(mktemp -d)
cp "$HERE"/app/* "$BUILD"/
echo "== signing with profile $PROFILE"
"$TIZEN" package -t wgt -s "$PROFILE" -- "$BUILD"
WGT=$(basename "$(ls "$BUILD"/*.wgt)")
echo "== installing $WGT"
"$TIZEN" install -n "$WGT" -t "$DEVICE" -- "$BUILD"
echo "== opening Clean Home"
"$TIZEN" run -p "$APP_ID" -t "$DEVICE"
