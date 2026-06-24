# Ubuntu usage for `slink_hz_monitor.py`

This monitor uses only Python 3 standard libraries. The only external command it
expects is `slinktool`.

## 1. Copy the script

Copy `slink_hz_monitor.py` to the Ubuntu machine, for example:

```bash
mkdir -p ~/slink-monitor
cp slink_hz_monitor.py ~/slink-monitor/
cp run_slink_monitor_ubuntu.sh ~/slink-monitor/
cd ~/slink-monitor
chmod +x slink_hz_monitor.py run_slink_monitor_ubuntu.sh
```

## 2. Check requirements

```bash
python3 --version
command -v slinktool
slinktool -Q 192.168.2.200 | grep TM | grep HZ
```

If `command -v slinktool` prints nothing, install or enable the SeedLink /
SeisComP package used at your site, then make sure the directory containing
`slinktool` is in `$PATH`.

## 3. Run the monitor

```bash
./slink_hz_monitor.py --server 192.168.2.200
```

Or use the Ubuntu launcher:

```bash
./run_slink_monitor_ubuntu.sh
```

Override defaults with environment variables:

```bash
SLINK_SERVER=192.168.2.200 ./run_slink_monitor_ubuntu.sh
```

By default, the monitor auto-detects every station from streams matching
`grep TM | grep HZ`. To limit the display to selected stations only, pass
`--stations` or set `SLINK_STATIONS`:

```bash
./slink_hz_monitor.py --server 192.168.2.200 --stations UBPT,CUSV
SLINK_STATIONS=UBPT,CUSV ./run_slink_monitor_ubuntu.sh
```

Equivalent mode using the exact shell pipeline:

```bash
./slink_hz_monitor.py \
  --shell-command 'slinktool -Q 192.168.2.200 | grep TM | grep HZ'
```

Useful options:

```bash
--slot-minutes 15       # time bucket size
--slots 8              # number of buckets shown
--poll-seconds 60      # query interval
--ok-lag-minutes 15    # green threshold
--warn-lag-minutes 30  # yellow threshold
--time-zone utc        # use utc or local for timestamps without timezone
--ascii                # use OK/!!/XX instead of colored dots
--log-file results.csv # append poll results to a CSV file
```

## 4. Save results continuously

To keep appending every poll result to a CSV file:

```bash
./slink_hz_monitor.py --server 192.168.2.200 --poll-seconds 900 --log-file slink_hz_results.csv
```

This writes one row per station per poll. The file keeps growing until you stop
the monitor with `Ctrl-c`.

CSV columns:

```text
poll_time,slot_start,server,network,channel,station,status,latest_packet_time,age_minutes,error
```

To watch the file while the monitor is running, open another terminal:

```bash
tail -f slink_hz_results.csv
```

Using the launcher:

```bash
SLINK_LOG_FILE=slink_hz_results.csv ./run_slink_monitor_ubuntu.sh --poll-seconds 900
```

By default, history is stored in:

```text
~/.local/state/slink_hz_monitor/state.json
```

## 5. Run inside tmux

```bash
tmux new -s slinkmon './slink_hz_monitor.py --server 192.168.2.200'
```

Detach with `Ctrl-b`, then `d`. Reconnect with:

```bash
tmux attach -t slinkmon
```

## 6. Optional systemd user service

Use this if you want the monitor to restart automatically and write output to
the user journal.

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/slink-hz-monitor.service
```

Paste this, adjusting paths if needed:

```ini
[Unit]
Description=SeedLink TM HZ station monitor

[Service]
Type=simple
WorkingDirectory=%h/slink-monitor
ExecStart=/usr/bin/python3 %h/slink-monitor/slink_hz_monitor.py --server 192.168.2.200 --poll-seconds 900 --slots 8 --ascii --log-file %h/slink-monitor/slink_hz_results.csv
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now slink-hz-monitor.service
systemctl --user status slink-hz-monitor.service
journalctl --user -u slink-hz-monitor.service -f
```

If `slinktool` is not found from systemd, replace `slinktool` with its absolute
path by using `--shell-command`, for example:

```ini
ExecStart=/usr/bin/python3 %h/slink-monitor/slink_hz_monitor.py --shell-command '/opt/seiscomp/bin/slinktool -Q 192.168.2.200 | grep TM | grep HZ' --ascii
```
