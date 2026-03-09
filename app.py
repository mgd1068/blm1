import json
import re
import subprocess
import platform
import threading
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml
import requests
import librouteros
from librouteros.exceptions import TrapError, FatalError
from flask import Flask, render_template, jsonify, request

# Suppress InsecureRequestWarning for self-signed OPNsense certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# Global state – written by background threads, read by Flask routes
device_status  = {}   # {device_id: "up" | "down"}
device_details = {}   # {device_id: {cpu, ram, uptime, ...}}
device_ping    = {}   # {device_id: {primary: {ip, reachable, latency}, alt?: ...}}
config         = {}   # full parsed config.yaml content

# Populated from config.yaml credentials at startup
MIKROTIK_USER       = ""   # read-only monitor account
MIKROTIK_PASS       = ""
MIKROTIK_WRITE_USER = ""   # write account for interface toggles
MIKROTIK_WRITE_PASS = ""

# How long (seconds) to wait for MikroTik / OPNsense API responses
API_TIMEOUT      = 8
# How often (seconds) to refresh detail data from device APIs
DETAILS_INTERVAL = 30

# Physical interface types reported by RouterOS that we want to display
MIKROTIK_PHYS_TYPES = {'ether', 'sfp', 'sfp-sfpplus', 'sfpplus', 'combo'}


# =============================================================================
# Config loading
# =============================================================================

def load_config():
    """Read config.yaml and populate global config and credential variables."""
    global config, MIKROTIK_USER, MIKROTIK_PASS, MIKROTIK_WRITE_USER, MIKROTIK_WRITE_PASS
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    # Pull monitor credentials out so the rest of the code can use simple
    # module-level constants instead of dict lookups everywhere.
    mon = config.get('credentials', {}).get('mikrotik_monitor', {})
    MIKROTIK_USER = mon.get('user', 'monitor')
    MIKROTIK_PASS = mon.get('password', 'monitor')

    wr = config.get('credentials', {}).get('mikrotik_write', {})
    MIKROTIK_WRITE_USER = wr.get('user', '')
    MIKROTIK_WRITE_PASS = wr.get('password', '')


# =============================================================================
# Ping helpers
# =============================================================================

def ping_host_with_latency(ip):
    """
    Ping a single host once and return (reachable: bool, latency_ms: float|None).
    Works on both Linux/macOS and Windows.
    """
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    cmd = ['ping', param, '1', ip]
    if platform.system().lower() != 'windows':
        cmd.extend(['-W', '1'])   # 1-second wait timeout on Linux
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if proc.returncode == 0:
            # Extract round-trip time from ping output (e.g. "time=1.23 ms")
            m = re.search(r'time[=<]\s*(\d+(?:\.\d+)?)', proc.stdout, re.IGNORECASE)
            if m:
                return True, round(float(m.group(1)), 1)
            return True, None   # reachable but RTT not parseable
        return False, None
    except Exception:
        return False, None


def get_alt_ip(ip):
    """
    Return the alternate 10.10.x.x address for a 10.20.x.x NatcoSwitch IP,
    or None if the address is not in the 10.20.0.0/16 range.
    """
    parts = ip.split('.')
    if len(parts) == 4 and parts[0] == '10' and parts[1] == '20':
        return f"10.10.{parts[2]}.{parts[3]}"
    return None


# =============================================================================
# RouterOS helpers
# =============================================================================

def parse_ros_duration(s):
    """
    Convert a RouterOS duration string like '1w2d3h4m5s' into total seconds.
    Returns None if the string is empty or unparseable.
    """
    if not s:
        return None
    total = 0
    for val, unit in re.findall(r'(\d+)([wdhms])', s):
        val = int(val)
        if unit == 'w':   total += val * 604800
        elif unit == 'd': total += val * 86400
        elif unit == 'h': total += val * 3600
        elif unit == 'm': total += val * 60
        elif unit == 's': total += val
    return total if total > 0 else None


# =============================================================================
# MikroTik data collection
# =============================================================================

def query_mikrotik(dev):
    """
    Connect to a MikroTik device via the RouterOS API (port 8728) and collect:
      - System resources (CPU, RAM, uptime)
      - WireGuard peer status
      - EoIP tunnel status
      - Physical interface list with speed
      - Local user accounts and SSH-key presence

    Returns a dict with all collected data, plus an 'error' key on failure.
    """
    result = {
        'cpu':          None,
        'ram_used_pct': None,
        'ram_free_mb':  None,
        'ram_total_mb': None,
        'uptime':       None,
        'wireguard':    [],
        'eoip':         [],
        'interfaces':   [],
        'bridges':      [],
        'users':        [],
        'error':        None,
    }
    try:
        api = librouteros.connect(
            dev['ip'],
            username=MIKROTIK_USER,
            password=MIKROTIK_PASS,
            port=8728,
            timeout=API_TIMEOUT,
        )

        # -- System resources ------------------------------------------------
        for r in api.path('system', 'resource'):
            result['cpu']          = int(r.get('cpu-load', 0))
            total                  = int(r.get('total-memory', 1))
            free                   = int(r.get('free-memory', 0))
            result['ram_total_mb'] = round(total / 1024 / 1024)
            result['ram_free_mb']  = round(free  / 1024 / 1024)
            result['ram_used_pct'] = round((total - free) / total * 100)
            result['uptime']       = r.get('uptime', 'N/A')

        # -- WireGuard peers -------------------------------------------------
        # A peer is considered "up" if its last handshake was < 3 minutes ago.
        try:
            for peer in api.path('interface', 'wireguard', 'peers'):
                last_hs = peer.get('last-handshake', '')
                secs    = parse_ros_duration(last_hs)
                name    = (peer.get('comment') or '').strip() or peer.get('interface', 'WG Peer')
                result['wireguard'].append({
                    'name':     name,
                    'endpoint': peer.get('current-endpoint-address', '—'),
                    'status':   'up' if (secs is not None and secs < 180) else 'down',
                })
        except (TrapError, FatalError):
            pass  # Device has no WireGuard configured – skip silently

        # -- Interface map (single pass) -------------------------------------
        # Build name→data dict so EoIP, bridge and physical interface sections
        # can all look up rx/tx counters without extra API round-trips.
        iface_map = {}
        try:
            for iface in api.path('interface'):
                iface_map[iface.get('name', '')] = iface
        except (TrapError, FatalError):
            pass

        # -- Physical interfaces (ether, sfp, combo …) ----------------------
        for name, iface in iface_map.items():
            if iface.get('type', '') not in MIKROTIK_PHYS_TYPES:
                continue
            if iface.get('disabled', False):
                continue
            running = iface.get('running', False)
            if isinstance(running, str):
                running = running.lower() == 'true'
            result['interfaces'].append({
                'name':    name,
                'comment': (iface.get('comment') or '').strip(),
                'status':  'up' if running else 'down',
                'speed':   '—',
            })

        # Enrich physical interfaces with negotiated link speed
        try:
            speed_map = {}
            for eth in api.path('interface', 'ethernet'):
                speed_map[eth.get('name', '')] = eth.get('rate', eth.get('speed', '—'))
            for iface in result['interfaces']:
                if iface['name'] in speed_map:
                    iface['speed'] = speed_map[iface['name']] or '—'
        except (TrapError, FatalError):
            pass

        # -- EoIP tunnels (config + traffic from iface_map) -----------------
        try:
            for tun in api.path('interface', 'eoip'):
                name    = tun.get('name', 'eoip')
                running = tun.get('running', False)
                if isinstance(running, str):
                    running = running.lower() == 'true'
                stats = iface_map.get(name, {})
                result['eoip'].append({
                    'name':    name,
                    'comment': (tun.get('comment') or '').strip(),
                    'remote':  tun.get('remote-address', '—'),
                    'status':  'up' if running else 'down',
                    'rx_byte': _safe_int(stats.get('rx-byte')),
                    'tx_byte': _safe_int(stats.get('tx-byte')),
                })
        except (TrapError, FatalError):
            pass

        # -- Bridges (with port membership and per-port traffic) ------------
        try:
            # Collect bridge port membership: bridge_name → [member_iface_name]
            bridge_ports = {}
            try:
                for port in api.path('interface', 'bridge', 'port'):
                    if port.get('disabled', False):
                        continue
                    bridge_ports.setdefault(
                        port.get('bridge', ''), []
                    ).append(port.get('interface', ''))
            except (TrapError, FatalError):
                pass

            for br in api.path('interface', 'bridge'):
                br_name = br.get('name', '')
                running = br.get('running', False)
                if isinstance(running, str):
                    running = running.lower() == 'true'
                stats = iface_map.get(br_name, {})

                # Build member port list with type, status and traffic
                ports = []
                for port_name in bridge_ports.get(br_name, []):
                    p = iface_map.get(port_name, {})
                    p_run = p.get('running', False)
                    if isinstance(p_run, str):
                        p_run = p_run.lower() == 'true'
                    ports.append({
                        'name':    port_name,
                        'comment': (p.get('comment') or '').strip(),
                        'type':    p.get('type', 'unknown'),
                        'status':  'up' if p_run else 'down',
                        'rx_byte': _safe_int(p.get('rx-byte')),
                        'tx_byte': _safe_int(p.get('tx-byte')),
                    })

                result['bridges'].append({
                    'name':    br_name,
                    'comment': (br.get('comment') or '').strip(),
                    'status':  'up' if running else 'down',
                    'rx_byte': _safe_int(stats.get('rx-byte')),
                    'tx_byte': _safe_int(stats.get('tx-byte')),
                    'ports':   ports,
                })
        except (TrapError, FatalError):
            pass

        # -- Users and SSH keys ----------------------------------------------
        try:
            # Collect which users have an SSH public key configured
            ssh_users = set()
            try:
                for key in api.path('user', 'ssh-keys'):
                    u = key.get('user', '')
                    if u:
                        ssh_users.add(u)
            except (TrapError, FatalError):
                pass  # No SSH keys table (older RouterOS)

            for user in api.path('user'):
                name = user.get('name', '')
                result['users'].append({
                    'name':        name,
                    'group':       user.get('group', ''),
                    'has_ssh_key': name in ssh_users,
                })
        except (TrapError, FatalError):
            pass

        api.close()

    except Exception as e:
        result['error'] = str(e)

    return result


# =============================================================================
# OPNsense data collection
# =============================================================================

def _opn_get(session, base, path, auth):
    """Issue an authenticated GET request to the OPNsense REST API."""
    r = session.get(f"{base}/{path}", auth=auth, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _opn_post(session, base, path, auth, data=None):
    """Issue an authenticated POST request to the OPNsense REST API."""
    r = session.post(f"{base}/{path}", auth=auth, timeout=API_TIMEOUT, json=data or {})
    r.raise_for_status()
    return r.json()


def _opn_sse_first(session, base, path, auth):
    """
    Read the first Server-Sent Events data frame from a streaming OPNsense
    endpoint and return it as a parsed dict.  Used for the CPU stream.
    """
    with session.get(f"{base}/{path}", auth=auth, stream=True,
                     timeout=API_TIMEOUT) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if raw and raw.startswith(b'data: '):
                return json.loads(raw[6:])
    return {}


def query_opnsense(dev):
    """
    Query an OPNsense firewall via its REST API and collect:
      - CPU utilisation (via SSE stream)
      - RAM usage
      - System uptime and load average
      - Firewall state table usage
      - Physical interface list with speed
      - WireGuard peer status (wg4–wg11)

    Returns a dict with all collected data, plus an 'error' key on failure.
    """
    result = {
        'cpu':          None,
        'ram_used_pct': None,
        'ram_free_mb':  None,
        'ram_total_mb': None,
        'uptime':       None,
        'loadavg':      None,
        'fw_states':    None,
        'fw_limit':     None,
        'wireguard':    [],
        'interfaces':   [],
        'error':        None,
    }

    base    = f"https://{dev['ip']}/api"
    auth    = (dev['api_key'], dev['api_secret'])  # credentials from config.yaml
    session = requests.Session()
    session.verify = False   # OPNsense typically uses a self-signed certificate
    errors  = []

    # -- CPU (first SSE event) -----------------------------------------------
    try:
        data = _opn_sse_first(session, base, 'diagnostics/cpu_usage/stream', auth)
        total_cpu = data.get('total', None)
        if total_cpu is None:
            # Fall back to 100 - idle if 'total' is not present
            idle = float(data.get('idle', 100))
            total_cpu = round(100 - idle)
        result['cpu'] = round(float(total_cpu))
    except Exception as e:
        errors.append(f"CPU: {e}")

    # -- RAM -----------------------------------------------------------------
    try:
        data  = _opn_get(session, base, 'diagnostics/system/system_resources', auth)
        mem   = data.get('memory', data)
        total = int(mem.get('total', 0))
        used  = int(mem.get('used', 0))
        if total > 0:
            result['ram_total_mb'] = round(total / 1024 / 1024)
            result['ram_free_mb']  = round((total - used) / 1024 / 1024)
            result['ram_used_pct'] = round(used / total * 100)
    except Exception as e:
        errors.append(f"RAM: {e}")

    # -- Uptime and load average ---------------------------------------------
    try:
        data = _opn_get(session, base, 'diagnostics/system/system_time', auth)
        result['uptime']  = data.get('uptime')
        result['loadavg'] = data.get('loadavg')
    except Exception as e:
        errors.append(f"Uptime: {e}")

    # -- Firewall state table ------------------------------------------------
    try:
        data = _opn_get(session, base, 'diagnostics/firewall/pf_states', auth)
        result['fw_states'] = data.get('current')
        result['fw_limit']  = data.get('limit')
    except Exception as e:
        errors.append(f"FW-States: {e}")

    # -- Physical interfaces -------------------------------------------------
    try:
        data = _opn_get(session, base, 'diagnostics/interface/get_interface_config', auth)

        # Interface name prefixes to skip (virtual/tunnel interfaces)
        SKIP_PREFIXES = ('lo', 'pfsync', 'enc', 'pflog', 'wg', 'ovpn',
                         'tun', 'tap', 'gif', 'gre', 'ipsec', '_')
        for name, info in data.items():
            if not isinstance(info, dict):
                continue
            if any(name.startswith(p) for p in SKIP_PREFIXES):
                continue

            status_raw = info.get('status', '')
            is_up = 'active' in status_raw.lower()

            media = info.get('media', '—')
            speed = _parse_bsd_media(media)

            descr = info.get('description', info.get('descr', '')).strip()

            result['interfaces'].append({
                'name':    name,
                'comment': descr,
                'status':  'up' if is_up else 'down',
                'speed':   speed,
            })
    except Exception as e:
        errors.append(f"Interfaces: {e}")

    # -- WireGuard peers (wg4–wg11 only) ------------------------------------
    try:
        data = _opn_get(session, base, 'wireguard/service/show', auth)
        rows = data.get('rows', [])

        for row in rows:
            if row.get('type') != 'peer':
                continue

            iface = row.get('if', '')
            m = re.match(r'^wg(\d+)$', iface)
            if not m or not (4 <= int(m.group(1)) <= 11):
                continue   # Only track wg4–wg11

            peer_status = row.get('peer-status', 'offline')
            is_up = peer_status == 'online'

            # Override with handshake age if available (< 180 s = up)
            hs_age = row.get('latest-handshake-age')
            if hs_age is not None:
                try:
                    is_up = int(hs_age) < 180
                except (ValueError, TypeError):
                    pass

            result['wireguard'].append({
                'interface': iface,
                'name':      row.get('name', '—'),
                'endpoint':  row.get('endpoint', '—'),
                'rx':        _fmt_bytes(row.get('transfer-rx', 0)),
                'tx':        _fmt_bytes(row.get('transfer-tx', 0)),
                'status':    'up' if is_up else 'down',
            })

    except Exception as e:
        errors.append(f"WireGuard: {e}")

    if errors:
        result['error'] = ' | '.join(errors)

    return result


# =============================================================================
# Utility / formatting helpers
# =============================================================================

def _parse_bsd_media(media):
    """
    Parse a BSD ifconfig media string like '1000baseT <full-duplex>'
    and return a human-readable speed string like '1 Gbps' or '100 Mbps'.
    """
    if not media or media == '—':
        return '—'
    m = re.search(r'(\d+)(?:base\S+)?', media)
    if not m:
        return media
    mbps = int(m.group(1))
    if mbps >= 1000:
        return f"{mbps // 1000} Gbps"
    return f"{mbps} Mbps"


def _safe_int(v):
    """Safely convert a value to int, returning 0 on failure."""
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def _fmt_bytes(b):
    """Format a byte count into a human-readable string (B / KB / MB / GB)."""
    try:
        b = int(b)
    except (ValueError, TypeError):
        return '—'
    if b >= 1_073_741_824:
        return f"{b/1_073_741_824:.1f} GB"
    if b >= 1_048_576:
        return f"{b/1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b/1024:.1f} KB"
    return f"{b} B"


# =============================================================================
# Background polling threads
# =============================================================================

def details_loop():
    """
    Background thread: query MikroTik and OPNsense devices for detailed metrics
    every DETAILS_INTERVAL seconds using a thread pool for parallelism.
    Results are stored in the global device_details dict.
    """
    global device_details
    while True:
        # Flatten all layers into a single list
        all_devices = []
        for layer in ('top_layer', 'middle_layer', 'bottom_layer', 'processing_layer'):
            all_devices.extend(config['devices'].get(layer, []))

        # Only query devices that have an API we know how to talk to
        queryable = [d for d in all_devices if d.get('type') in ('mikrotik', 'opnsense')]

        temp = {}
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {}
            for dev in queryable:
                if dev['type'] == 'mikrotik':
                    futures[pool.submit(query_mikrotik, dev)] = dev['id']
                elif dev['type'] == 'opnsense':
                    futures[pool.submit(query_opnsense, dev)] = dev['id']

            for future in as_completed(futures):
                dev_id = futures[future]
                try:
                    temp[dev_id] = future.result()
                except Exception as e:
                    temp[dev_id] = {'error': str(e)}

        device_details = temp
        time.sleep(DETAILS_INTERVAL)


def monitor_loop():
    """
    Background thread: ping all devices every ping_interval seconds.
    NatcoSwitches (10.20.x.x) are pinged on both primary and alternate IPs.
    Results are stored in device_status and device_ping globals.
    """
    global device_status, device_ping
    while True:
        # Flatten all layers
        all_devices = []
        for layer in ('top_layer', 'middle_layer', 'bottom_layer', 'processing_layer'):
            all_devices.extend(config['devices'].get(layer, []))

        # Build ping task list: (device_id, kind, ip)
        tasks = []
        for dev in all_devices:
            tasks.append((dev['id'], 'primary', dev['ip']))
            alt = get_alt_ip(dev['ip'])
            if alt:
                tasks.append((dev['id'], 'alt', alt))

        # Run all pings in parallel
        results = {}
        with ThreadPoolExecutor(max_workers=max(len(tasks), 1)) as pool:
            futures = {
                pool.submit(ping_host_with_latency, ip): (dev_id, kind, ip)
                for dev_id, kind, ip in tasks
            }
            for future in as_completed(futures):
                dev_id, kind, ip = futures[future]
                reachable, latency = future.result()
                if dev_id not in results:
                    results[dev_id] = {}
                results[dev_id][kind] = {
                    'ip':        ip,
                    'reachable': reachable,
                    'latency':   latency,
                }

        # A device is "up" if its primary IP is reachable
        device_status = {
            dev_id: ('up' if data.get('primary', {}).get('reachable', False) else 'down')
            for dev_id, data in results.items()
        }
        device_ping = results
        time.sleep(config['settings']['ping_interval'])


# =============================================================================
# Flask routes
# =============================================================================

@app.route('/')
def index():
    """Render the main dashboard page."""
    return render_template('index.html', config=config)


@app.route('/api/status')
def get_status():
    """Return per-device up/down status as JSON."""
    return jsonify(device_status)


@app.route('/api/details')
def get_details():
    """Return detailed metrics for all queryable devices as JSON."""
    return jsonify(device_details)


@app.route('/api/ping')
def get_ping():
    """Return ping results (IP, reachability, latency) for all devices as JSON."""
    return jsonify(device_ping)


@app.route('/api/interface/toggle', methods=['POST'])
def toggle_interface():
    """
    Toggle the disabled state of a physical Ethernet interface on a MikroTik device.

    Request JSON:
        device_id  – device id as defined in config.yaml
        interface  – interface name (e.g. 'ether9')

    Response JSON:
        { "disabled": <bool>, "interface": <name> }   on success
        { "error": <message> }                         on failure

    Requires credentials.mikrotik_write in config.yaml.
    Only ethernet-type interfaces may be toggled (safety guard).
    """
    if not MIKROTIK_WRITE_USER:
        return jsonify({'error': 'No write account configured (credentials.mikrotik_write missing)'}), 503

    body       = request.get_json(force=True) or {}
    dev_id     = body.get('device_id', '').strip()
    iface_name = body.get('interface', '').strip()

    if not dev_id or not iface_name:
        return jsonify({'error': 'device_id and interface are required'}), 400

    # Look up device
    dev = next((
        d for layer in config['devices'].values()
        for d in layer
        if d.get('id') == dev_id and d.get('type') == 'mikrotik'
    ), None)
    if not dev:
        return jsonify({'error': f'MikroTik device "{dev_id}" not found'}), 404

    try:
        api = librouteros.connect(
            dev['ip'],
            username=MIKROTIK_WRITE_USER,
            password=MIKROTIK_WRITE_PASS,
            port=8728,
            timeout=API_TIMEOUT,
        )

        # Find the interface and verify it is an ethernet type (safety guard)
        target = None
        for iface in api.path('interface'):
            if iface.get('name') == iface_name:
                target = iface
                break

        if target is None:
            api.close()
            return jsonify({'error': f'Interface "{iface_name}" not found on {dev_id}'}), 404

        # Only allow toggling physical ethernet interfaces
        allowed_types = {'ether', 'sfp', 'sfp-sfpplus', 'sfpplus', 'combo'}
        if target.get('type', '') not in allowed_types:
            api.close()
            return jsonify({'error': f'Interface "{iface_name}" is not an Ethernet interface (type: {target.get("type")})'}), 400

        # Determine current disabled state
        current_disabled = target.get('disabled', False)
        if isinstance(current_disabled, str):
            current_disabled = current_disabled.lower() == 'true'

        new_disabled = not current_disabled

        # Apply the change
        api.path('interface').update(**{
            '.id':      target['.id'],
            'disabled': 'yes' if new_disabled else 'no',
        })
        api.close()

        return jsonify({'interface': iface_name, 'disabled': new_disabled})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Entry point
# =============================================================================

if __name__ == '__main__':
    load_config()
    # Start background polling threads as daemons so they die with the main process
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=details_loop,  daemon=True).start()
    print("Starting Benslab Monitor on http://0.0.0.0:5000 ...")
    app.run(host='0.0.0.0', port=5000, debug=False)
