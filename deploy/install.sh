#!/bin/sh
# Eseguito come root sulla macchina di destinazione da deploy.sh.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)

missing=""
command -v v4l2-ctl >/dev/null || missing="$missing v4l-utils"
python3 -c 'import PIL' 2>/dev/null || missing="$missing python3-pil"
if [ -n "$missing" ]; then
    apt-get install -y $missing
fi

rm -rf /opt/mysee
mkdir -p /opt/mysee
cp -r "$SRC/mysee/." /opt/mysee/
install -m 644 "$SRC/deploy/mysee.service" /etc/systemd/system/mysee.service
systemctl daemon-reload
systemctl enable mysee >/dev/null 2>&1
systemctl restart mysee
sleep 2
systemctl --no-pager --lines=5 status mysee
