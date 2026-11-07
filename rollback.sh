#!/bin/sh
set -eu

APP_DIR=/home/ntp-gateway
BACKUP_DIR="$APP_DIR/backup"
INIT_FILE=/etc/init.d/S55ntp-gateway
AUTORUN=/etc/htset/autorun

echo "Stopping ntp-gateway"
"$INIT_FILE" stop 2>/dev/null || true
rm -f "$INIT_FILE"

if [ -f "$BACKUP_DIR/autorun.openGW" ]; then
    cp -p "$BACKUP_DIR/autorun.openGW" "$AUTORUN"
    chmod 755 "$AUTORUN"
    echo "Restored original autorun"
else
    echo "No original autorun backup found" >&2
    exit 1
fi

echo "Starting openGWEnterprise"
cd /home/openGW
./openGWEnterprise.sh >/tmp/openGWEnterprise.rollback.log 2>&1 &
sleep 3
if pidof openGWEnterprise >/dev/null 2>&1; then
    echo "openGWEnterprise restored"
else
    echo "openGWEnterprise did not start; inspect /tmp/openGWEnterprise.rollback.log" >&2
    exit 1
fi
