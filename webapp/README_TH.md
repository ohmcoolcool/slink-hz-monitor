# Web App: SeedLink TM/HZ Monitor

เว็บแอปนี้เป็น dashboard สำหรับดูผล `slinktool -Q <server>` ที่กรองด้วย `TM` และ `HZ`

แนวคิดหลัก:

- `poll` คือรอบที่ไป query จริง เช่น ทุก 60 วินาที
- `slot` คือหน่วยสรุปผลบนตาราง เช่น ทุก 15 นาที
- history grid ใช้ best status ภายใน slot
- current status ใช้ poll ล่าสุด
- alert/error ใช้สถานะระบบล่าสุด ไม่ปนกับ slot summary

## Run on Ubuntu

```bash
cd slink-hz-monitor/webapp
chmod +x monitor_web.py run_web_monitor_ubuntu.sh
./run_web_monitor_ubuntu.sh
```

จากเครื่องอื่นในวง LAN เปิด:

```text
http://<ubuntu-ip>:8000
```

ถ้าเครื่อง Ubuntu คือ `192.168.2.50`:

```text
http://192.168.2.50:8000
```

## Run with explicit options

```bash
./monitor_web.py --host 0.0.0.0 --port 8000 --server 192.168.2.200 --poll-seconds 60 --slot-minutes 15
```

## Check before running

```bash
python3 --version
command -v slinktool
slinktool -Q 192.168.2.200 | grep TM | grep HZ
```

## Useful environment variables

```bash
SLINK_SERVER=192.168.2.200
SLINK_WEB_HOST=0.0.0.0
SLINK_WEB_PORT=8000
SLINK_POLL_SECONDS=60
SLINK_SLOT_MINUTES=15
SLINK_DB=./data/monitor.db
```

Example:

```bash
SLINK_SERVER=192.168.2.200 SLINK_POLL_SECONDS=60 ./run_web_monitor_ubuntu.sh
```

## API

```text
GET  /api/health
GET  /api/status/latest
GET  /api/status/slots?hours=8
GET  /api/stations
GET  /api/stations/<station>/history?hours=24
GET  /api/export.csv?hours=24
POST /api/poll-now
```

## Systemd service example

Create:

```bash
nano ~/.config/systemd/user/slink-web-monitor.service
```

Paste:

```ini
[Unit]
Description=SeedLink TM/HZ web monitor

[Service]
Type=simple
WorkingDirectory=%h/slink-hz-monitor/webapp
ExecStart=/usr/bin/python3 %h/slink-hz-monitor/webapp/monitor_web.py --host 0.0.0.0 --port 8000 --server 192.168.2.200 --poll-seconds 60 --slot-minutes 15
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Enable:

```bash
systemctl --user daemon-reload
systemctl --user enable --now slink-web-monitor.service
systemctl --user status slink-web-monitor.service
```

View logs:

```bash
journalctl --user -u slink-web-monitor.service -f
```
