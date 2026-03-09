#!/bin/bash
# ============================================================
#  Benslab – MikroTik Switch Check & Setup
#  Verifies and configures:
#    - User 'monitor' (group: monitoring, read+ssh+api+test)
#    - API service on port 8728 (persistent)
#
#  All credentials are read from config.yaml.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python3"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Python venv not found at $PYTHON"
    exit 1
fi

"$PYTHON" - <<'PYEOF'
import socket
import sys
import yaml
import paramiko

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
with open('config.yaml', 'r') as f:
    CONFIG = yaml.safe_load(f)

_mon = CONFIG['credentials']['mikrotik_monitor']
MONITOR_USER  = _mon['user']
MONITOR_PASS  = _mon['password']
MONITOR_GROUP = "monitoring"
API_PORT      = 8728
SSH_TIMEOUT   = 5

ADMIN_CREDS = CONFIG['credentials'].get('mikrotik_admin', {})

def build_devices():
    devices = []
    for layer in ('top_layer', 'middle_layer', 'bottom_layer', 'processing_layer'):
        for dev in CONFIG['devices'].get(layer, []):
            cred_key = dev.get('admin_cred')
            if not cred_key:
                continue
            creds = ADMIN_CREDS.get(cred_key)
            if not creds:
                print(f"WARNING: admin_cred '{cred_key}' for {dev['id']} not in config.yaml")
                continue
            devices.append({
                'name': dev['name'],
                'ip':   dev['ip'],
                'user': creds['user'],
                'pw':   creds['password'],
            })
    return devices

DEVICES = build_devices()

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
OK   = f"{GREEN}✓{RESET}"
FAIL = f"{RED}✗{RESET}"
SKIP = f"{YELLOW}·{RESET}"

results = {"ok": 0, "fail": 0}

# ---------------------------------------------------------------------------
# SSH helper
# ---------------------------------------------------------------------------
def run(client, cmd):
    """Run a command over SSH and return stdout as a string."""
    _, out, err = client.exec_command(cmd, timeout=SSH_TIMEOUT)
    out.channel.settimeout(SSH_TIMEOUT)
    try:
        return out.read().decode("utf-8", errors="replace").strip()
    except socket.timeout:
        return ""

# ---------------------------------------------------------------------------
# Per-device check
# ---------------------------------------------------------------------------
def check(dev):
    name = dev["name"]
    pad  = f"  [{name:<10}]"
    errors = []

    print(f"\n  {BOLD}{CYAN}{name}{RESET}  ({dev['ip']})")
    print(f"  {'─' * 48}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            dev["ip"],
            username=dev["user"],
            password=dev["pw"],
            timeout=SSH_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
        print(f"{pad} {OK}   SSH connected")
    except Exception as e:
        print(f"{pad} {FAIL} SSH failed: {e}")
        results["fail"] += 1
        return

    try:
        # -- Monitoring group ------------------------------------------------
        exists = run(client, f":put [:len [/user group find name={MONITOR_GROUP}]]")
        if exists == "0":
            run(client,
                f"/user group add name={MONITOR_GROUP} "
                f"policy=read,ssh,api,test "
                f'comment="read-only monitoring"')
            print(f"{pad} {OK}   Group '{MONITOR_GROUP}' created")
        else:
            run(client, f"/user group set [find name={MONITOR_GROUP}] policy=read,ssh,api,test")
            print(f"{pad} {SKIP}  Group '{MONITOR_GROUP}' exists – policies verified")

        # -- Monitor user ----------------------------------------------------
        exists = run(client, f":put [:len [/user find name={MONITOR_USER}]]")
        if exists == "0":
            err = run(client,
                f"/user add name={MONITOR_USER} password={MONITOR_PASS} "
                f'group={MONITOR_GROUP} comment="monitoring read-only"')
            if err:
                print(f"{pad} {FAIL} Create user: {err}")
                errors.append("user")
            else:
                print(f"{pad} {OK}   User '{MONITOR_USER}' created")
        else:
            run(client,
                f"/user set [find name={MONITOR_USER}] "
                f"password={MONITOR_PASS} group={MONITOR_GROUP}")
            print(f"{pad} {SKIP}  User '{MONITOR_USER}' exists – updated")

        # -- API service -----------------------------------------------------
        disabled = run(client, ":put [/ip service get api disabled]")
        port     = run(client, ":put [/ip service get api port]")

        if disabled.lower() == "true" or port != str(API_PORT):
            run(client, f"/ip service set api disabled=no port={API_PORT}")
            print(f"{pad} {OK}   API service enabled (port {API_PORT})")
        else:
            print(f"{pad} {SKIP}  API service active on port {port}")

        # -- Verification ----------------------------------------------------
        u = run(client, f":put [:len [/user find name={MONITOR_USER}]]")
        g = run(client, f":put [/user group get [find name={MONITOR_GROUP}] policy]")
        d = run(client, ":put [/ip service get api disabled]")
        p = run(client, ":put [/ip service get api port]")

        user_ok = u != "0"
        api_ok  = d.lower() == "false" and p == str(API_PORT)

        status = OK if (user_ok and api_ok and not errors) else FAIL
        print(f"{pad} {status}  Verify: user={user_ok}  api_active={api_ok}  port={p}")
        print(f"  {'':13}  Policies: {g}")

        if user_ok and api_ok and not errors:
            results["ok"] += 1
        else:
            results["fail"] += 1

    except Exception as e:
        print(f"{pad} {FAIL} Error: {e}")
        results["fail"] += 1
    finally:
        client.close()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
print(f"\n{BOLD}{'═'*52}{RESET}")
print(f"{BOLD}  Benslab – MikroTik Switch Check & Setup{RESET}")
print(f"{BOLD}{'═'*52}{RESET}")

if not DEVICES:
    print("\nNo devices with admin_cred found in config.yaml. Nothing to do.")
    sys.exit(0)

for dev in DEVICES:
    check(dev)

total = len(DEVICES)
print(f"\n{'═'*52}")
print(f"  Result: {GREEN}{results['ok']} OK{RESET}  |  {RED}{results['fail']} errors{RESET}  |  {total} devices total")
print(f"{'═'*52}\n")

sys.exit(0 if results["fail"] == 0 else 1)
PYEOF
