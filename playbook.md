# OpenWebRX+ Headless FT8 PSKReporter Playbook

OpenWebRX+ runs headless — no desktop, no browser required. It automatically decodes FT8 (and FT4, WSPR) at startup and reports spots to PSKReporter continuously.

**Adapt these values to your setup before running anything:**

| Variable | Example | Description |
|---|---|---|
| `YOUR_CALLSIGN` | `YOUR_CALLSIGN` | Your PSKReporter reporting callsign |
| `YOUR_LAT` | `50.8157` | Receiver latitude (decimal degrees) |
| `YOUR_LON` | `-0.1374` | Receiver longitude (decimal degrees, negative = West) |
| `YOUR_SDR_SERIAL` | `YOUR_SDR_SERIAL` | RTL-SDR serial number (set with `rtl_eeprom -s`) |
| `YOUR_6M_PROFILE_KEY` | `9ce11cac-…` | UUID of the 6m profile in your settings.json |

---

## How it works

OpenWebRX+ runs as a systemd service. Three settings in `/var/lib/openwebrx/settings.json` cause it to start the SDR and run FT8 decoding with no browser connected:

| Setting | Location | Value | Effect |
|---|---|---|---|
| `always-on` | `sdrs.rtlsdr` | `true` | Starts the SDR at service startup |
| `services` | `sdrs.rtlsdr` | `true` | Enables background decoders on this device |
| `services_enabled` | top-level | `true` | Enables background decoding globally |

The active profile (frequency/bandwidth) is determined by the **first profile in the `sdrs.rtlsdr.profiles` dict**. The 6m profile must remain first.

PSKReporter reporting requires `receiver_lat`, `receiver_lon` and `receiver_gps` to be set at the **top level** of settings.json. `receiver_gps` is a dict `{"lat": ..., "lon": ...}` used to compute the Maidenhead locator for upload packets.

A watchdog runs every 30 minutes via systemd timer, validates all settings, and restarts openwebrx if `rtl_connector` stops or `decoded.txt` goes stale.

### WSJTX 2.x compatibility patches (CRITICAL)

OpenWebRX+ 1.2.x was designed for jt9 1.x which wrote decoded results to stdout. WSJTX 2.x jt9 writes only `<DecodeFinished>` to stdout — decoded results go to `decoded.txt` file. Two files must be patched after any `apt upgrade` of openwebrx:

**`/usr/lib/python3/dist-packages/owrx/audio/queue.py`** — `QueueJob.run()` has two fixes:
1. Read decodes from `decoded.txt` instead of stdout.
2. Truncate `decoded.txt` to 0 bytes **before** each jt9 run. jt9 2.x overwrites the file (not appends), so without this the `pre_size` mechanism seeks to the old file size and reads nothing — causing `pskreporter_spots_total` to stay permanently at 0 even when decodes are happening.

**`/usr/lib/python3/dist-packages/owrx/wsjt.py`** — `Jt9Decoder.parse()` must handle the new format (extra `depth` field, decimal freq, `MODE` suffix).

The patch script lives permanently at `/usr/local/bin/patch_owrx.py`. To re-apply after an upgrade:

`sudo python3 /usr/local/bin/patch_owrx.py && sudo systemctl restart openwebrx`

The script is idempotent — safe to run multiple times. Source of truth is this repo (`patch_owrx.py`).

---

## Key settings in /var/lib/openwebrx/settings.json

### SDR device block

```json
"sdrs": {
    "rtlsdr": {
        "name": "RTL-SDR",
        "type": "rtl_sdr",
        "device": "YOUR_SDR_SERIAL",
        "enabled": true,
        "always-on": true,
        "services": true,
        "profiles": {
            "YOUR_6M_PROFILE_KEY": {
                "name": "6m",
                "rf_gain": "auto",
                "center_freq": 50900000,
                "samp_rate": 2400000,
                "start_freq": 50313000,
                "start_mod": "usb",
                "tuning_step": 100
            },
            ... other profiles ...
        }
    }
}
```

### Top-level reporting settings

```json
"services_enabled": true,
"services_decoders": ["ft8", "ft4", "wspr", "packet"],
"pskreporter_enabled": true,
"pskreporter_callsign": "YOUR_CALLSIGN",
"pskreporter_antenna_information": "Whip",
"pskreporter_rig_information": "openwebrx+",
"receiver_lat": YOUR_LAT,
"receiver_lon": YOUR_LON
```

`receiver_lat`/`receiver_lon` are required for PSKReporter to construct valid upload packets. Without them the uploader runs but sends nothing.

---

## Applying the settings (fresh VM or after reset)

SSH into your OpenWebRX+ VM and run:

```python
sudo python3 - <<'EOF'
import json

with open('/var/lib/openwebrx/settings.json') as f:
    d = json.load(f)

# Enable always-on and background services on the SDR device
d['sdrs']['rtlsdr']['always-on'] = True
d['sdrs']['rtlsdr']['services'] = True

# Enable background decoding globally
d['services_enabled'] = True
d['services_decoders'] = ['ft8', 'ft4', 'wspr', 'packet']

# Move 6m profile to the front of the profiles dict (determines startup frequency)
profiles = d['sdrs']['rtlsdr']['profiles']
sixm_key = 'YOUR_6M_PROFILE_KEY'
new_profiles = {sixm_key: profiles[sixm_key]}
for k, v in profiles.items():
    if k != sixm_key:
        new_profiles[k] = v
d['sdrs']['rtlsdr']['profiles'] = new_profiles

with open('/var/lib/openwebrx/settings.json', 'w') as f:
    json.dump(d, f, indent=4)

print('Done. First profile:', list(new_profiles.values())[0]['name'])
EOF
```

Also set receiver coordinates:

```python
sudo python3 -c "import json; d=json.load(open('/var/lib/openwebrx/settings.json')); d['receiver_lat']=YOUR_LAT; d['receiver_lon']=YOUR_LON; open('/var/lib/openwebrx/settings.json','w').write(json.dumps(d,indent=4))"
```

Then restart the service:

`sudo systemctl restart openwebrx`

---

## Watchdog

A systemd timer runs `/usr/local/bin/owrx-watchdog.sh` every 30 minutes. It:
- Validates all required settings in `settings.json` and fixes any that are wrong
- Checks `rtl_connector` is running (continuous process); restarts openwebrx if not
- Checks `decoded.txt` was updated in the last 5 minutes; restarts openwebrx if stale
- Logs all actions to `/var/log/owrx-watchdog.log`

### Deploy watchdog on fresh VM

```bash
sudo tee /usr/local/bin/owrx-watchdog.sh << 'SCRIPT'
#!/bin/bash
SETTINGS=/var/lib/openwebrx/settings.json
LOGFILE=/var/log/owrx-watchdog.log
SIXM_PROFILE="YOUR_6M_PROFILE_KEY"
RESTART_NEEDED=0
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE"; }
python3 - <<EOF >> "$LOGFILE" 2>&1
import json, sys
SIXM = "$SIXM_PROFILE"
changed = []
with open("$SETTINGS") as f:
    d = json.load(f)
sdr = d["sdrs"]["rtlsdr"]
if not sdr.get("always-on"): sdr["always-on"] = True; changed.append("always-on")
if not sdr.get("services"): sdr["services"] = True; changed.append("services")
if not d.get("services_enabled"): d["services_enabled"] = True; changed.append("services_enabled")
if d.get("receiver_lat") is None: d["receiver_lat"] = YOUR_LAT; changed.append("receiver_lat")
if d.get("receiver_lon") is None: d["receiver_lon"] = YOUR_LON; changed.append("receiver_lon")
if not d.get("pskreporter_enabled"): d["pskreporter_enabled"] = True; changed.append("pskreporter_enabled")
profiles = sdr["profiles"]
if list(profiles.keys())[0] != SIXM and SIXM in profiles:
    new = {SIXM: profiles[SIXM]}
    new.update({k:v for k,v in profiles.items() if k!=SIXM})
    sdr["profiles"] = new; changed.append("6m-profile-order")
if changed:
    open("$SETTINGS","w").write(json.dumps(d,indent=4))
    print("Fixed:", ", ".join(changed)); sys.exit(1)
sys.exit(0)
EOF
SETTINGS_EXIT=$?
[ $SETTINGS_EXIT -ne 0 ] && log "Settings corrected, restarting" && RESTART_NEEDED=1
pgrep -f "rtl_connector" > /dev/null 2>&1 || { log "jt9 --ft8 not running, restarting"; RESTART_NEEDED=1; }
systemctl is-active --quiet openwebrx || { log "openwebrx down, starting"; RESTART_NEEDED=1; }
[ $RESTART_NEEDED -eq 1 ] && systemctl restart openwebrx && log "restarted" || log "OK"
SCRIPT
sudo chmod +x /usr/local/bin/owrx-watchdog.sh
```

```bash
sudo tee /etc/systemd/system/owrx-watchdog.service << 'EOF'
[Unit]
Description=OpenWebRX+ FT8 watchdog
After=openwebrx.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/owrx-watchdog.sh
EOF
```

```bash
sudo tee /etc/systemd/system/owrx-watchdog.timer << 'EOF'
[Unit]
Description=Run OpenWebRX+ FT8 watchdog every 60 seconds
[Timer]
OnBootSec=90
OnUnitActiveSec=1800
AccuracySec=10
[Install]
WantedBy=timers.target
EOF
```

`sudo systemctl daemon-reload && sudo systemctl enable --now owrx-watchdog.timer`

---

## Verifying it works

Check the SDR started on 6m (50.9 MHz center):

`sudo journalctl -u openwebrx -n 5 --no-pager | grep 'Started sdr'`

Expected output contains `-f 50900000`.

Check FT8 decoder is running:

`ps aux | grep 'jt9 --ft8' | grep -v grep`

Expected: a `jt9 --ft8 -d` process.

PSKReporter uploads every 5 minutes. Check for your callsign spots at https://pskreporter.info/pskmap.html

---

## Important: profile order

OpenWebRX+ uses the **first profile in the dict** as the startup profile when `always-on` is set. There is no explicit `start_profile` setting in v1.2.x. If the web admin interface is used to save SDR settings, it may reorder the profiles and revert to 2m (the original first profile).

After any web admin save, re-run the Python snippet above to restore 6m to the front.

---

## Troubleshooting

**SDR not starting after reboot** — check `always-on` is still set: `sudo python3 -c "import json; d=json.load(open('/var/lib/openwebrx/settings.json')); print(d['sdrs']['rtlsdr'].get('always-on'))"`

**SDR starts on 2m instead of 6m** — the profile order was reset. Re-run the Python snippet to move 6m back to first.

**FT8 decoder not running** — check `services` and `services_enabled` are both `true`. Restart openwebrx.

**No spots on PSKReporter** — Check `receiver_gps` is set: `sudo python3 -c "import json; d=json.load(open('/var/lib/openwebrx/settings.json')); print(d.get('receiver_gps'))"`. PSKReporter batches every 5 minutes. 6m FT8 is a daytime band — no propagation = nothing to report. Verify patches are in place: `sudo python3 /usr/local/bin/patch_owrx.py` (will say "already patched" if OK). Check metrics: `curl -s http://localhost:8073/metrics | grep psk` — `pskreporter_spots_total` should be climbing. If `pskreporter_spots_total` stays at 0 after restarting despite `wsjt_decodes_6m_FT8_total` climbing, the queue.py truncation patch is missing — re-run the patch script.

**Verify UDP packets are being sent** — `sudo tcpdump -i any -n 'udp and port 4739'` for 6 minutes. Should see one packet per 5-minute cycle.

**Check watchdog log** — `sudo tail -20 /var/log/owrx-watchdog.log`

**Check decode metrics** — `curl -s http://localhost:8073/metrics | grep -E 'psk|wsjt|decod'` — shows spots queued, decodes per band/mode, queue depth.
