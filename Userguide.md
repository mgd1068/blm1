# Benslab Monitor – User Guide

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11 or newer |
| Network access | Must be able to reach all monitored device IPs |
| MikroTik devices | RouterOS API enabled (port 8728); monitor user created |
| OPNsense devices | REST API enabled; API key + secret generated |

---

## Installation

### 1. Prepare the directory

```bash
mkdir -p /opt/blm1
cd /opt/blm1
# Copy or clone project files here
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Required packages: `flask`, `requests`, `urllib3`, `librouteros`, `paramiko`, `pyyaml`

---

## Configuration

All settings live in **`config.yaml`**. Open it in any text editor.

### Settings section

```yaml
settings:
  ping_interval: 5   # seconds between ping cycles
  timeout: 1         # ping timeout per host
```

### Credentials section

```yaml
credentials:
  mikrotik_monitor:
    user: monitor
    password: <your-monitor-password>

  mikrotik_admin:
    benslab:
      user: admin
      password: <benslab-admin-password>
    natco:
      user: admin
      password: <natco-admin-password>
```

- `mikrotik_monitor` – read-only account used by the Flask app at runtime
- `mikrotik_admin.*` – admin accounts used only by `setup_switches.py` / `check_switches.sh`

### Adding a device

Each device entry requires at minimum `id`, `name`, `ip`, and `type`.

```yaml
devices:
  middle_layer:
    - id: ben3              # unique identifier (used internally and in API responses)
      name: Benslab3        # display name shown in the dashboard
      ip: 192.168.200.132   # IP address to ping and query
      type: mikrotik        # mikrotik | opnsense | none
      admin_cred: benslab   # optional: references credentials.mikrotik_admin.benslab
```

#### For OPNsense devices, add API credentials:

```yaml
    - id: opn3
      name: OPNSense3
      ip: 192.168.202.1
      type: opnsense
      api_key: "your-api-key"
      api_secret: "your-api-secret"
```

#### `type: none` devices

Devices with `type: none` are only pinged – no API query is made and no detail modal opens in the UI. Use this for switches or servers where you have no API access (e.g. pfSense, terminal servers).

---

## First-time MikroTik Setup

Before the monitor can query MikroTik devices via the RouterOS API, each switch needs:
1. A `monitoring` group with `read,ssh,api,test` permissions
2. A `monitor` user in that group
3. The RouterOS API service enabled on port 8728

The provisioning script does all of this automatically:

```bash
cd /opt/blm1
source venv/bin/activate
python setup_switches.py
```

Or use the bash wrapper (which also verifies after setup):

```bash
./check_switches.sh
```

Both scripts read device IPs and credentials from `config.yaml`. Only devices with an `admin_cred` field are processed.

**Expected output:**

```
╔══════════════════════════════════════════════════╗
║   Benslab – MikroTik Switch Setup                ║
╚══════════════════════════════════════════════════╝

────────────────────────────────────────────────────
  Benslab1  (192.168.200.130)
────────────────────────────────────────────────────
  [Benslab1  ] ✓ SSH connected as 'admin'
  [Benslab1  ] ✓ Group 'monitoring' created  (read,ssh,api,test)
  [Benslab1  ] ✓ User 'monitor' created
  [Benslab1  ] ✓ API service enabled  (port 8728, persistent)
  [Benslab1  ] · Verify: user=True, api_disabled=false, api_port=8728
```

---

## OPNsense API Setup

1. In OPNsense, go to **System → Access → Users**
2. Create a user (or use an existing one) and add API key credentials
3. Assign the user a group with read-only access to `Diagnostics`, `Firewall`, `WireGuard`
4. Copy the generated **API Key** and **API Secret** into `config.yaml`

---

## Starting the Monitor

```bash
cd /opt/blm1
./start.sh
```

Or directly:

```bash
./venv/bin/python app.py
```

The server listens on `http://0.0.0.0:5000`. Open a browser and navigate to:

```
http://<server-ip>:5000
```

To run as a background service, see [Operations.md](Operations.md).

---

## Using the Dashboard

### Device tiles

Each monitored device appears as a tile. The tile border colour indicates reachability:

| Border colour | Meaning |
|---|---|
| Green | Device is up (primary IP reachable) |
| Red | Device is down (primary IP unreachable) |
| Grey | Status unknown (first poll not yet complete) |

The tile header shows a small CPU arc gauge. The tile body shows:
- **CPU** – current load percentage
- **RAM** – used percentage and free MB
- **Uptime**

### Device detail modal

Click any tile for a device with `type: mikrotik` or `type: opnsense` to open the detail modal. It shows:

**MikroTik:**
- System resources (CPU, RAM gauge, uptime)
- WireGuard peer list with status and endpoint
- EoIP tunnel list with remote address and status
- Physical interface list with speed and link status
- Local user accounts and SSH-key indicator

**OPNsense:**
- System resources (CPU, RAM gauge, uptime, load average)
- Firewall state table (used / limit)
- Physical interface list with speed and link status
- WireGuard peer list (wg4–wg11) with RX/TX counters

### Summary donut chart

The header contains a donut chart summarising all devices:
- Green segment = devices up
- Red segment = devices down

### Ping latency

For NatcoSwitches, the tile footer shows latency for both the primary (10.20.x.x) and alternate (10.10.x.x) path.

---

## REST API

The Flask app exposes three JSON endpoints for scripting or integration:

### `GET /api/status`

Returns the up/down status of all devices.

```json
{
  "opn1": "up",
  "opn2": "up",
  "cz": "down"
}
```

### `GET /api/details`

Returns full detail data for all queryable devices.

```json
{
  "opn1": {
    "cpu": 12,
    "ram_used_pct": 45,
    "ram_free_mb": 2048,
    "ram_total_mb": 4096,
    "uptime": "10 days 04:22:11",
    "loadavg": "0.12 0.08 0.05",
    "fw_states": 1234,
    "fw_limit": 100000,
    "wireguard": [...],
    "interfaces": [...],
    "error": null
  }
}
```

### `GET /api/ping`

Returns raw ping results per device.

```json
{
  "cz": {
    "primary": {"ip": "10.20.4.2", "reachable": true, "latency": 2.3},
    "alt":     {"ip": "10.10.4.2", "reachable": false, "latency": null}
  }
}
```
