#!/usr/bin/env python3
"""
setup_switches.py – One-time provisioning script for MikroTik switches.

Creates the read-only 'monitor' user and group and enables the RouterOS API
service on every device listed in config.yaml that has an admin_cred entry.

Run this once after adding a new switch to config.yaml, or to verify that
existing switches are still correctly configured.
"""

import socket
import sys
import yaml
import paramiko

# ---------------------------------------------------------------------------
# Load configuration from config.yaml
# ---------------------------------------------------------------------------

with open('config.yaml', 'r') as f:
    CONFIG = yaml.safe_load(f)

# Read the monitor account that the Flask app uses at runtime
_mon = CONFIG['credentials']['mikrotik_monitor']
MONITOR_USER  = _mon['user']
MONITOR_PASS  = _mon['password']
MONITOR_GROUP = "monitoring"

_wr = CONFIG['credentials'].get('mikrotik_write', {})
WRITE_USER  = _wr.get('user', '')
WRITE_PASS  = _wr.get('password', '')
WRITE_GROUP = "netops"

API_PORT    = 8728
SSH_TIMEOUT = 5

# Build admin credential lookup: group name → {user, password}
ADMIN_CREDS = CONFIG['credentials'].get('mikrotik_admin', {})

# Build device list from all layers; only include devices with admin_cred set
def _build_device_list():
    devices = []
    for layer in ('top_layer', 'middle_layer', 'bottom_layer', 'processing_layer'):
        for dev in CONFIG['devices'].get(layer, []):
            cred_key = dev.get('admin_cred')
            if not cred_key:
                continue   # No admin credentials configured – skip
            creds = ADMIN_CREDS.get(cred_key)
            if not creds:
                print(f"WARNING: admin_cred '{cred_key}' for {dev['id']} not found in config.yaml")
                continue
            devices.append({
                'name': dev['name'],
                'ip':   dev['ip'],
                'user': creds['user'],
                'pw':   creds['password'],
            })
    return devices

DEVICES = _build_device_list()

OK   = "✓"
FAIL = "✗"
INFO = "·"


# ---------------------------------------------------------------------------
# SSH helper
# ---------------------------------------------------------------------------

def ssh_run(client, cmd):
    """Execute a command over SSH and return (stdout, stderr) as strings."""
    _, stdout, stderr = client.exec_command(cmd, timeout=SSH_TIMEOUT)
    stdout.channel.settimeout(SSH_TIMEOUT)
    try:
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
    except socket.timeout:
        out, err = "", "timeout"
    return out, err


# ---------------------------------------------------------------------------
# Per-device setup
# ---------------------------------------------------------------------------

def setup(dev):
    """
    Connect to a MikroTik device via SSH and ensure:
      1. A 'monitoring' group exists with policy read,ssh,api,test
      2. The monitor user exists with the configured password
      3. The RouterOS API service is enabled on port 8728
    """
    pad = f"  [{dev['name']:10}]"
    print(f"\n{'─'*52}")
    print(f"  {dev['name']}  ({dev['ip']})")
    print(f"{'─'*52}")

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
        print(f"{pad} {OK} SSH connected as '{dev['user']}'")
    except Exception as e:
        print(f"{pad} {FAIL} SSH failed: {e}")
        return

    try:
        # -- 1. Monitoring group ---------------------------------------------
        out, _ = ssh_run(client, f":put [:len [/user group find name={MONITOR_GROUP}]]")
        if out == "0":
            _, err = ssh_run(client,
                f'/user group add name={MONITOR_GROUP} '
                f'policy=read,ssh,api,test '
                f'comment="read-only monitoring"'
            )
            if err and "already" not in err.lower():
                print(f"{pad} {FAIL} Create group: {err}")
            else:
                print(f"{pad} {OK} Group '{MONITOR_GROUP}' created  (read,ssh,api,test)")
        else:
            # Group exists – ensure policies are still correct
            ssh_run(client,
                f'/user group set [find name={MONITOR_GROUP}] '
                f'policy=read,ssh,api,test'
            )
            print(f"{pad} {INFO} Group '{MONITOR_GROUP}' exists – policies verified")

        # -- 2. Monitor user -------------------------------------------------
        out, _ = ssh_run(client, f":put [:len [/user find name={MONITOR_USER}]]")
        if out == "0":
            _, err = ssh_run(client,
                f'/user add name={MONITOR_USER} password={MONITOR_PASS} '
                f'group={MONITOR_GROUP} comment="monitoring read-only"'
            )
            if err:
                print(f"{pad} {FAIL} Create user: {err}")
            else:
                print(f"{pad} {OK} User '{MONITOR_USER}' created")
        else:
            _, err = ssh_run(client,
                f'/user set [find name={MONITOR_USER}] '
                f'password={MONITOR_PASS} group={MONITOR_GROUP}'
            )
            if err:
                print(f"{pad} {FAIL} Update user: {err}")
            else:
                print(f"{pad} {INFO} User '{MONITOR_USER}' exists – password & group verified")

        # -- 3. API service --------------------------------------------------
        disabled, _ = ssh_run(client, ":put [/ip service get api disabled]")
        port,     _ = ssh_run(client, ":put [/ip service get api port]")

        if disabled.lower() == "true" or port != str(API_PORT):
            _, err = ssh_run(client, f"/ip service set api disabled=no port={API_PORT}")
            if err:
                print(f"{pad} {FAIL} Enable API service: {err}")
            else:
                print(f"{pad} {OK} API service enabled  (port {API_PORT}, persistent)")
        else:
            print(f"{pad} {INFO} API service already active on port {API_PORT}")

        # -- 4. Write (netops) group and user --------------------------------
        if WRITE_USER:
            out, _ = ssh_run(client, f":put [:len [/user group find name={WRITE_GROUP}]]")
            if out == "0":
                ssh_run(client,
                    f'/user group add name={WRITE_GROUP} '
                    f'policy=api,write,read '
                    f'comment="netops write access"'
                )
                print(f"{pad} {OK} Group '{WRITE_GROUP}' created  (api,write,read)")
            else:
                ssh_run(client, f'/user group set [find name={WRITE_GROUP}] policy=api,write,read')
                print(f"{pad} {INFO} Group '{WRITE_GROUP}' exists – policies verified")

            out, _ = ssh_run(client, f":put [:len [/user find name={WRITE_USER}]]")
            if out == "0":
                _, err = ssh_run(client,
                    f'/user add name={WRITE_USER} password={WRITE_PASS} '
                    f'group={WRITE_GROUP} comment="netops write account"'
                )
                print(f"{pad} {OK if not err else FAIL} User '{WRITE_USER}' {'created' if not err else 'error: ' + err}")
            else:
                ssh_run(client,
                    f'/user set [find name={WRITE_USER}] '
                    f'password={WRITE_PASS} group={WRITE_GROUP}'
                )
                print(f"{pad} {INFO} User '{WRITE_USER}' exists – password & group verified")

        # -- 5. Verification -------------------------------------------------
        user_ok, _      = ssh_run(client, f":put [:len [/user find name={MONITOR_USER}]]")
        api_dis, _      = ssh_run(client, ":put [/ip service get api disabled]")
        api_port_now, _ = ssh_run(client, ":put [/ip service get api port]")
        print(f"{pad} {INFO} Verify: user={user_ok != '0'}, "
              f"api_disabled={api_dis}, api_port={api_port_now}")

    except Exception as e:
        print(f"{pad} {FAIL} Unexpected error: {e}")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════════╗")
    print("║   Benslab – MikroTik Switch Setup                ║")
    print("╚══════════════════════════════════════════════════╝")
    if not DEVICES:
        print("\nNo devices with admin_cred found in config.yaml. Nothing to do.")
        sys.exit(0)
    for dev in DEVICES:
        setup(dev)
    print(f"\n{'═'*52}")
    print("  Setup complete.")
    print(f"{'═'*52}\n")
