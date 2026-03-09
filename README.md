# Benslab Monitor

Internes Netzwerk-Monitoring-Dashboard für das Benslab. Nicht für externe Nutzung vorgesehen.

## Was es macht

- Pingt alle Benslab-Geräte alle 5 Sekunden und zeigt Up/Down-Status
- Fragt MikroTik-Geräte über die RouterOS API ab: CPU, RAM, Uptime, WireGuard, EoIP, Interfaces, User
- Fragt OPNsense-Firewalls über die REST API ab: CPU, RAM, Uptime, Loadavg, Firewall-States, WireGuard, Interfaces
- Pingt NatcoSwitches (10.20.x.x) auf beiden Pfaden (primär + alternativ 10.10.x.x)
- Dark-Theme Dashboard im Browser auf Port 5000

## Starten

```bash
cd /opt/blm1
./start.sh
```

Dashboard: `http://<server-ip>:5000`

## Konfiguration

Alle Geräte, Credentials und Einstellungen in `config.yaml` (nicht im Repo – liegt nur lokal).
Vorlage: `config.yaml.template`

## Dokumentation

- [Userguide.md](Userguide.md) – Einrichtung und Bedienung
- [Operations.md](Operations.md) – Betrieb, Konfigurationsreferenz, Troubleshooting
