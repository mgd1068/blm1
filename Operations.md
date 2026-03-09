# Benslab Monitor – Operations Guide

## Running as a systemd Service

Create a unit file so the monitor starts automatically on boot:

```bash
sudo nano /etc/systemd/system/blm1.service
```

```ini
[Unit]
Description=Benslab Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/blm1
ExecStart=/opt/blm1/venv/bin/python /opt/blm1/app.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now blm1
sudo systemctl status blm1
```

**Useful commands:**

```bash
sudo systemctl start blm1      # start
sudo systemctl stop blm1       # stop
sudo systemctl restart blm1    # restart (e.g. after config change)
journalctl -u blm1 -f          # follow logs
```

---

## config.yaml Reference

### `settings`

| Key | Type | Default | Description |
|---|---|---|---|
| `ping_interval` | int | `5` | Seconds between ping cycles for all devices |
| `timeout` | int | `1` | Ping wait timeout per host in seconds |

### `credentials.mikrotik_monitor`

| Key | Description |
|---|---|
| `user` | Username for the read-only RouterOS API account |
| `password` | Password for the read-only RouterOS API account |

### `credentials.mikrotik_admin.<group>`

Named groups of admin credentials used by `setup_switches.py` and `check_switches.sh`.
Each device in `devices` may reference a group via its `admin_cred` field.

| Key | Description |
|---|---|
| `user` | SSH admin username |
| `password` | SSH admin password |

### `devices.<layer>[]`

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique device identifier (alphanumeric, no spaces) |
| `name` | yes | Human-readable display name for the dashboard |
| `ip` | yes | Primary IP address (used for ping and API queries) |
| `type` | yes | `mikrotik` / `opnsense` / `none` |
| `api_key` | OPNsense only | OPNsense REST API key |
| `api_secret` | OPNsense only | OPNsense REST API secret |
| `admin_cred` | optional | Key into `credentials.mikrotik_admin` for setup scripts |

### Layers and their roles

| Layer key | Displayed as | Typical devices |
|---|---|---|
| `top_layer` | Top row | Firewalls |
| `middle_layer` | Middle row | Core switches |
| `bottom_layer` | Bottom row | Site / NatcoSwitches |
| `processing_layer` | Bottom row (2nd) | Processing nodes, terminal servers |

---

## Adding a New Device

1. Open `config.yaml`
2. Add an entry to the appropriate layer
3. If it is a MikroTik device: run `python setup_switches.py` or `./check_switches.sh` to provision the monitor user
4. Restart the monitor: `sudo systemctl restart blm1`

The dashboard updates automatically – no frontend changes are needed.

---

## Polling Intervals

| What | Interval | Controlled by |
|---|---|---|
| ICMP ping (up/down, latency) | `settings.ping_interval` (default 5 s) | `monitor_loop` thread |
| API detail queries (CPU, RAM, etc.) | 30 s (hardcoded `DETAILS_INTERVAL`) | `details_loop` thread |
| Frontend refresh | 5 s (JavaScript `setInterval`) | `templates/index.html` |

To change the detail poll interval, edit `DETAILS_INTERVAL` at the top of `app.py`.

---

## Credential Management

### Rotating the MikroTik monitor password

1. Update `credentials.mikrotik_monitor.password` in `config.yaml`
2. Run `./check_switches.sh` – it will push the new password to all devices
3. Restart the monitor: `sudo systemctl restart blm1`

### Rotating OPNsense API keys

1. In OPNsense, revoke the old key and generate a new one
2. Update `api_key` and `api_secret` for the affected device in `config.yaml`
3. Restart the monitor: `sudo systemctl restart blm1`

### Rotating MikroTik admin passwords

1. Change the password on the device via WinBox / SSH
2. Update `credentials.mikrotik_admin.<group>.password` in `config.yaml`
3. No restart required (admin credentials are only used by setup scripts, not by the running app)

---

## Security Hardening

### File permissions

```bash
chmod 600 /opt/blm1/config.yaml   # only root can read credentials
chmod 700 /opt/blm1               # only root can list directory
```

### MikroTik: switch to API-SSL

The RouterOS API on port 8728 is unencrypted. For production, enable API-SSL on port 8729:

```
/ip service set api-ssl disabled=no port=8729
/ip service set api disabled=yes
```

Then change `port=8728` to `port=8729` in `app.py` (`librouteros.connect`) and pass `ssl=True`.

### OPNsense: enable TLS verification

OPNsense uses a self-signed certificate by default, so `session.verify = False` is set in `app.py`. To enable verification:

1. Export the OPNsense CA certificate
2. Save it as `/opt/blm1/opnsense-ca.pem`
3. In `app.py`, change `session.verify = False` to `session.verify = '/opt/blm1/opnsense-ca.pem'`

### Network segmentation

Ensure the monitoring server can reach:
- MikroTik devices on port 8728 (TCP) – or 8729 for API-SSL
- OPNsense devices on port 443 (TCP)
- All device IPs for ICMP (ping)

Restrict inbound access to port 5000 to trusted hosts only (firewall rule or reverse proxy with authentication).

---

## Troubleshooting

### Device shows "down" but is reachable

- Verify the IP in `config.yaml` matches the actual device IP
- Test manually: `ping -c 1 <ip>`
- Check that the monitoring server is not blocked by a firewall ACL

### MikroTik device shows error in modal

Common causes:
- Monitor user does not exist → run `setup_switches.py`
- RouterOS API service disabled → run `setup_switches.py`
- Wrong password in `credentials.mikrotik_monitor` → update and restart
- Device unreachable on port 8728 → check firewall rules

Test connectivity manually:
```bash
/opt/blm1/venv/bin/python -c "
import librouteros
api = librouteros.connect('192.168.200.130', username='monitor', password='monitor', port=8728, timeout=5)
print(list(api.path('system', 'resource')))
api.close()
"
```

### OPNsense device shows error in modal

Common causes:
- Wrong API key or secret → update `config.yaml` and restart
- REST API not enabled in OPNsense → System → Settings → Administration → enable API
- Network block on port 443 from monitoring server

Test manually:
```bash
curl -sk -u "API_KEY:API_SECRET" https://192.168.200.1/api/diagnostics/system/system_resources
```

### Flask app fails to start

```bash
journalctl -u blm1 -n 50
```

Common causes:
- `config.yaml` syntax error → validate with `python -c "import yaml; yaml.safe_load(open('config.yaml'))"`
- Missing Python package → `pip install -r requirements.txt`
- Port 5000 already in use → `ss -tlnp | grep 5000`

---

## Upgrading

```bash
cd /opt/blm1
sudo systemctl stop blm1
# Replace app.py / templates / etc.
source venv/bin/activate
pip install -r requirements.txt   # in case new packages were added
sudo systemctl start blm1
```

---

## Backup

The only files that need to be backed up are:

| File | Contains |
|---|---|
| `config.yaml` | All device definitions and credentials |
| `app.py` | Application logic |
| `templates/index.html` | Dashboard frontend |

The `venv/` directory can always be recreated with `pip install -r requirements.txt`.
