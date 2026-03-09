# Benslab Monitor

A lightweight, real-time network monitoring dashboard for home-lab / small enterprise environments built with Flask.

## Features

- **Live device status** – colour-coded up/down tiles updated every 5 seconds via ping
- **Deep MikroTik metrics** – CPU, RAM, uptime, WireGuard peers, EoIP tunnels, physical interfaces, users
- **OPNsense metrics** – CPU, RAM, uptime, load average, firewall state-table usage, WireGuard peers, physical interfaces
- **Dual-path ping** – NatcoSwitches (10.20.x.x) are pinged on both primary and alternate (10.10.x.x) paths simultaneously
- **Dark dashboard UI** – responsive card grid with SVG arc gauges and Chart.js donut charts
- **JSON REST API** – `/api/status`, `/api/details`, `/api/ping` for integration with other tools

## Supported Device Types

| Type | Layer | Examples |
|---|---|---|
| OPNsense | top | opn1, opn2 |
| pfSense / other (ping only) | top | pfs |
| MikroTik core switches | middle | ben1, ben2, ben11 |
| MikroTik site switches (NatcoSwitches) | bottom | cz, sk, pl, gr, me, mk, hr, at |
| MikroTik processing / terminal server | processing | bensp, ts |

## Quick Start

```bash
# 1. Clone / copy to /opt/blm1
cd /opt/blm1

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Edit configuration
cp config.yaml config.yaml   # already present; edit credentials & devices

# 5. (First time) provision MikroTik switches
python setup_switches.py

# 6. Start the monitor
./start.sh
# or:  venv/bin/python app.py
```

Open `http://<server-ip>:5000` in a browser.

## Architecture

```
┌─────────────┐   HTTP :5000   ┌───────────────┐
│   Browser   │ ◄────────────► │   Flask app   │  app.py
└─────────────┘                │   (app.py)    │
                               └──────┬────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    │                 │                   │
              monitor_loop      details_loop         Flask routes
              (ping, 5 s)      (API poll, 30 s)    /  /api/*
                    │                 │
            ┌───────┴───────┐  ┌─────┴──────┐
            │   subprocess  │  │ librouteros │  MikroTik API (8728)
            │  ping (ICMP)  │  │  requests   │  OPNsense REST API (HTTPS)
            └───────────────┘  └────────────┘
```

## Project Files

| File | Purpose |
|---|---|
| `app.py` | Flask backend – polling threads, API queries, HTTP routes |
| `config.yaml` | All device definitions, credentials and settings |
| `templates/index.html` | Single-page dashboard frontend (Jinja2 + vanilla JS) |
| `requirements.txt` | Python dependencies |
| `start.sh` | Convenience start script |
| `setup_switches.py` | One-time MikroTik provisioning script |
| `check_switches.sh` | Bash wrapper around the same provisioning logic |

## Security Notes

- `config.yaml` contains API keys and admin passwords – restrict file permissions (`chmod 600 config.yaml`)
- MikroTik API runs on port 8728 (plaintext); consider switching to 8729 (API-SSL) for production
- OPNsense TLS certificate verification is disabled (`verify=False`) because OPNsense ships with a self-signed cert; replace with a trusted cert or pin the CA to enable verification
- The monitor MikroTik account has `read,ssh,api,test` permissions only – no write access

## License

Internal / home-lab use.
