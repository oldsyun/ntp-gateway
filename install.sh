#!/bin/sh
set -eu

SRC_DIR=$(cd "$(dirname "$0")" && pwd)
APP_DIR=/home/ntp-gateway
BACKUP_DIR="$APP_DIR/backup"
INIT_FILE=/etc/init.d/S55ntp-gateway
AUTORUN=/etc/htset/autorun

echo "[1/7] prepare directories"
mkdir -p "$APP_DIR/logs" "$BACKUP_DIR"

if [ -f "$AUTORUN" ] && [ ! -f "$BACKUP_DIR/autorun.openGW" ]; then
    cp -p "$AUTORUN" "$BACKUP_DIR/autorun.openGW"
fi
if [ -d /home/openGW ] && [ ! -f "$BACKUP_DIR/openGWEnterprise.sh" ]; then
    cp -p /home/openGW/openGWEnterprise.sh "$BACKUP_DIR/openGWEnterprise.sh" 2>/dev/null || true
fi

echo "[2/7] stop existing openGWEnterprise"
for pid in $(pidof goWDTARM 2>/dev/null || true); do
    kill -9 "$pid" 2>/dev/null || true
done
sleep 1
for pid in $(pidof openGWEnterprise 2>/dev/null || true); do
    kill -9 "$pid" 2>/dev/null || true
done
for pid in $(ps w 2>/dev/null | grep '[o]penGWEnterprise.sh' | awk '{print $1}'); do
    kill -9 "$pid" 2>/dev/null || true
done

echo "[3/7] install application"
cp -a "$SRC_DIR"/app.py "$SRC_DIR"/config_manager.py "$SRC_DIR"/crc16.py \
      "$SRC_DIR"/ntp_client.py "$SRC_DIR"/p3_protocol.py "$SRC_DIR"/port_worker.py \
      "$SRC_DIR"/schedule_utils.py "$SRC_DIR"/serial_port.py \
      "$SRC_DIR"/serial_discovery.py \
      "$SRC_DIR"/web_server.py "$SRC_DIR"/network_manager.py \
      "$SRC_DIR"/default_config.json "$SRC_DIR"/hardware_profile.example.json \
      "$SRC_DIR"/install.sh "$SRC_DIR"/rollback.sh \
      "$SRC_DIR"/README.md "$SRC_DIR"/DEPLOYMENT.md "$APP_DIR"/
rm -rf "$APP_DIR/web.new"
cp -a "$SRC_DIR"/web "$APP_DIR/web.new"
rm -rf "$APP_DIR/web"
mv "$APP_DIR/web.new" "$APP_DIR/web"
chmod 755 "$APP_DIR/app.py" "$APP_DIR/install.sh" "$APP_DIR/rollback.sh"
chmod 644 "$APP_DIR"/*.py "$APP_DIR/default_config.json"
chmod -R u=rwX,go=rX "$APP_DIR/web"

if [ -f "$SRC_DIR/hardware_profile.json" ] && [ ! -f "$APP_DIR/hardware_profile.json" ]; then
    cp -p "$SRC_DIR/hardware_profile.json" "$APP_DIR/hardware_profile.json"
fi

echo "[4/7] validate configuration and initialise credentials"
cd "$APP_DIR"
if [ ! -f "$APP_DIR/config.json" ]; then
    echo "No config.json yet; serial ports will be generated from this hardware:"
    /usr/bin/python3 "$APP_DIR/app.py" --list-serial || true
fi
/usr/bin/python3 "$APP_DIR/app.py" --check
chmod 600 "$APP_DIR/config.json" "$APP_DIR/initial-password.txt" 2>/dev/null || true

echo "[5/7] install init script"
cat > "$INIT_FILE" <<'EOF'
#!/bin/sh
APP_DIR=/home/ntp-gateway
PID_FILE=/run/ntp-gateway.pid
LOG_FILE=/home/ntp-gateway/logs/console.log

is_running() {
    [ -f "$PID_FILE" ] || return 1
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    [ -n "$PID" ] || return 1
    kill -0 "$PID" 2>/dev/null
}

start() {
    if is_running; then
        echo "ntp-gateway already running pid=$(cat "$PID_FILE")"
        return 0
    fi
    mkdir -p "$APP_DIR/logs"
    cd "$APP_DIR"
    nohup /usr/bin/python3 "$APP_DIR/app.py" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if ! is_running; then
        echo "ntp-gateway failed to start"
        tail -n 50 "$LOG_FILE" 2>/dev/null || true
        return 1
    fi
    echo "ntp-gateway started pid=$(cat "$PID_FILE")"
}

stop() {
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "ntp-gateway not running"
        return 0
    fi
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null || true
    COUNT=0
    while kill -0 "$PID" 2>/dev/null && [ "$COUNT" -lt 10 ]; do
        sleep 1
        COUNT=$((COUNT + 1))
    done
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "ntp-gateway stopped"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) if is_running; then echo "running pid=$(cat "$PID_FILE")"; else echo "stopped"; exit 1; fi ;;
    *) echo "usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
EOF
chmod 755 "$INIT_FILE"

echo "[6/7] disable openGWEnterprise auto-start"
cat > "$AUTORUN" <<'EOF'
#!/bin/sh
# openGWEnterprise is disabled by the standalone NTP gateway installation.
# The original startup script is backed up at:
# /home/ntp-gateway/backup/autorun.openGW
case "${1:-start}" in
  start)
    /etc/init.d/S55ntp-gateway start
    ;;
  stop)
    /etc/init.d/S55ntp-gateway stop
    ;;
esac
exit 0
EOF
chmod 755 "$AUTORUN"

echo "[7/7] start standalone service"
"$INIT_FILE" stop >/dev/null 2>&1 || true
"$INIT_FILE" start
sleep 1
sync

echo
echo "Installation complete."
WEB_IP=$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -n 1)
[ -n "$WEB_IP" ] || WEB_IP=192.168.50.138
echo "Web:      http://$WEB_IP/"
echo "Password: $APP_DIR/initial-password.txt"
echo "Log:      $APP_DIR/logs/gateway.log"
echo "Rollback: $APP_DIR/rollback.sh"
