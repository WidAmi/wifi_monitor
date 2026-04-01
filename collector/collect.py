#!/usr/bin/env python3
"""
Collect WiFi client, mesh peer, and wired port stats from OpenWRT APs.
Writes metrics to InfluxDB. Run as a systemd oneshot via network-collector.timer.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import yaml
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

BASE_DIR = Path(__file__).parent.parent
DOTENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "config.yaml"

# Sent to each AP over stdin. Section markers delimit output for local parsing.
COLLECTION_SCRIPT = """\
echo '### WIRELESS_STATUS ###'
ubus call network.wireless status 2>/dev/null || echo '{}'
echo '### CLIENTS_phy1-ap0 ###'
ubus call hostapd.phy1-ap0 get_clients 2>/dev/null || echo '{}'
echo '### CLIENTS_phy2-ap0 ###'
ubus call hostapd.phy2-ap0 get_clients 2>/dev/null || echo '{}'
echo '### MESH_DUMP ###'
iw dev phy0-mesh0 station dump 2>/dev/null
echo '### STATION_DUMP_phy1-ap0 ###'
iw dev phy1-ap0 station dump 2>/dev/null
echo '### STATION_DUMP_phy2-ap0 ###'
iw dev phy2-ap0 station dump 2>/dev/null
echo '### SELF_MESH_MAC ###'
cat /sys/class/net/phy0-mesh0/address 2>/dev/null
echo '### NET_DEV ###'
cat /proc/net/dev
echo '### END ###'
"""


def load_env():
    env = {}
    if DOTENV_PATH.exists():
        for line in DOTENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def ssh_collect(hostname, user, key_path, timeout):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname, username=user, key_filename=key_path, timeout=timeout)
    stdin, stdout, _ = client.exec_command("sh -s")
    stdin.write(COLLECTION_SCRIPT)
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", errors="replace")
    client.close()
    return output


def parse_sections(output):
    """Split output into named sections by ### SECTION_NAME ### markers."""
    sections = {}
    current = None
    lines = []
    for line in output.splitlines():
        if line.startswith("### ") and line.endswith(" ###"):
            if current is not None:
                sections[current] = "\n".join(lines)
            current = line[4:-4]
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines)
    return sections


def parse_wireless_status(json_str):
    """Return dict of interface_name -> band ('2g' or '5g')."""
    iface_band = {}
    try:
        data = json.loads(json_str)
        for radio in data.values():
            band = radio.get("config", {}).get("band", "")
            for iface in radio.get("interfaces", []):
                ifname = iface.get("ifname", "")
                if ifname:
                    iface_band[ifname] = band
    except (json.JSONDecodeError, AttributeError):
        pass
    return iface_band


def parse_clients(json_str, ap, ifname, band, timestamp):
    points = []
    try:
        data = json.loads(json_str)
        clients = data.get("clients", {})
    except (json.JSONDecodeError, TypeError):
        clients = {}

    band_label = "5GHz" if band == "5g" else "2.4GHz"

    points.append(
        Point("wifi_clients")
        .tag("ap", ap)
        .tag("interface", ifname)
        .tag("band", band_label)
        .field("client_count", len(clients))
        .time(timestamp)
    )

    for mac, info in clients.items():
        if not isinstance(info, dict):
            continue

        if info.get("he"):
            wifi_gen = 6
        elif info.get("vht"):
            wifi_gen = 5
        elif info.get("ht"):
            wifi_gen = 4
        else:
            wifi_gen = 3

        rate = info.get("rate", {})
        byt = info.get("bytes", {})
        pkt = info.get("packets", {})
        airtime = info.get("airtime", {})

        points.append(
            Point("wifi_station")
            .tag("ap", ap)
            .tag("interface", ifname)
            .tag("band", band_label)
            .tag("mac", mac)
            .field("signal_dbm", info.get("signal", 0))
            .field("tx_rate_mbps", rate.get("tx", 0) / 1_000_000)
            .field("rx_rate_mbps", rate.get("rx", 0) / 1_000_000)
            .field("tx_bytes", byt.get("tx", 0))
            .field("rx_bytes", byt.get("rx", 0))
            .field("tx_packets", pkt.get("tx", 0))
            .field("rx_packets", pkt.get("rx", 0))
            .field("tx_airtime_us", airtime.get("tx", 0))
            .field("rx_airtime_us", airtime.get("rx", 0))
            .field("wifi_gen", wifi_gen)
            .time(timestamp)
        )

    return points


def parse_mesh_dump(text, ap, mac_to_ap, timestamp):
    points = []
    current_mac = None
    fields = {}

    for line in text.splitlines():
        m = re.match(r"^Station ([0-9a-f:]{17})", line)
        if m:
            if current_mac and fields:
                points.append(_make_mesh_point(ap, current_mac, fields, mac_to_ap, timestamp))
            current_mac = m.group(1)
            fields = {}
            continue

        if current_mac is None:
            continue

        line = line.strip()

        for pattern, key, cast in [
            (r"signal:\s+([-\d]+)", "signal_dbm", int),
            (r"tx bytes:\s+(\d+)", "tx_bytes", int),
            (r"rx bytes:\s+(\d+)", "rx_bytes", int),
            (r"tx packets:\s+(\d+)", "tx_packets", int),
            (r"rx packets:\s+(\d+)", "rx_packets", int),
            (r"tx retries:\s+(\d+)", "tx_retries", int),
            (r"tx failed:\s+(\d+)", "tx_failed", int),
            (r"mesh airtime link metric:\s+(\d+)", "airtime_link_metric", int),
            (r"tx bitrate:\s+([\d.]+)", "tx_rate_mbps", float),
            (r"rx bitrate:\s+([\d.]+)", "rx_rate_mbps", float),
        ]:
            m = re.match(pattern, line)
            if m:
                fields[key] = cast(m.group(1))
                break

        m = re.match(r"mesh plink:\s+(\w+)", line)
        if m:
            fields["plink_state"] = m.group(1)

    if current_mac and fields:
        points.append(_make_mesh_point(ap, current_mac, fields, mac_to_ap, timestamp))

    return points


def _make_mesh_point(ap, mac, fields, mac_to_ap, timestamp):
    peer_ap = mac_to_ap.get(mac, mac)  # fall back to raw MAC if unknown
    return (
        Point("mesh_peer")
        .tag("ap", ap)
        .tag("interface", "phy0-mesh0")
        .tag("peer_mac", mac)
        .tag("peer_ap", peer_ap)
        .field("signal_dbm", fields.get("signal_dbm", 0))
        .field("tx_rate_mbps", fields.get("tx_rate_mbps", 0.0))
        .field("rx_rate_mbps", fields.get("rx_rate_mbps", 0.0))
        .field("tx_bytes", fields.get("tx_bytes", 0))
        .field("rx_bytes", fields.get("rx_bytes", 0))
        .field("tx_packets", fields.get("tx_packets", 0))
        .field("rx_packets", fields.get("rx_packets", 0))
        .field("tx_retries", fields.get("tx_retries", 0))
        .field("tx_failed", fields.get("tx_failed", 0))
        .field("airtime_link_metric", fields.get("airtime_link_metric", 0))
        .field("plink_state", fields.get("plink_state", "UNKNOWN"))
        .time(timestamp)
    )


def parse_client_retries(text, ap, ifname, band, timestamp):
    """Parse iw station dump, write one point per client MAC with tx_retries and tx_failed."""
    points = []
    current_mac = None
    retries = failed = 0

    for line in text.splitlines():
        m = re.match(r"^Station ([0-9a-f:]{17})", line)
        if m:
            if current_mac is not None:
                points.append(
                    Point("client_retries")
                    .tag("ap", ap)
                    .tag("interface", ifname)
                    .tag("band", band)
                    .tag("mac", current_mac)
                    .field("tx_retries", retries)
                    .field("tx_failed", failed)
                    .time(timestamp)
                )
            current_mac = m.group(1)
            retries = failed = 0
            continue

        line = line.strip()
        m = re.match(r"tx retries:\s+(\d+)", line)
        if m:
            retries = int(m.group(1))
        m = re.match(r"tx failed:\s+(\d+)", line)
        if m:
            failed = int(m.group(1))

    if current_mac is not None:
        points.append(
            Point("client_retries")
            .tag("ap", ap)
            .tag("interface", ifname)
            .tag("band", band)
            .tag("mac", current_mac)
            .field("tx_retries", retries)
            .field("tx_failed", failed)
            .time(timestamp)
        )

    return points


def parse_net_dev(text, ap, timestamp):
    points = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, stats = line.split(":", 1)
        iface = iface.strip()
        if iface not in ("wan", "lan1", "lan2", "lan3"):
            continue
        vals = stats.split()
        if len(vals) < 16:
            continue
        try:
            points.append(
                Point("wired_port")
                .tag("ap", ap)
                .tag("port", iface)
                .field("rx_bytes", int(vals[0]))
                .field("rx_packets", int(vals[1]))
                .field("rx_errors", int(vals[2]))
                .field("rx_drops", int(vals[3]))
                .field("tx_bytes", int(vals[8]))
                .field("tx_packets", int(vals[9]))
                .field("tx_errors", int(vals[10]))
                .field("tx_drops", int(vals[11]))
                .time(timestamp)
            )
        except (ValueError, IndexError):
            continue
    return points


def parse_ap_data(ap, sections, mac_to_ap, timestamp):
    iface_band = parse_wireless_status(sections.get("WIRELESS_STATUS", "{}"))
    iface_band.setdefault("phy1-ap0", "2g")
    iface_band.setdefault("phy2-ap0", "5g")

    points = []
    for ifname in ("phy1-ap0", "phy2-ap0"):
        points += parse_clients(
            sections.get(f"CLIENTS_{ifname}", "{}"),
            ap, ifname, iface_band[ifname], timestamp,
        )

    points += parse_mesh_dump(sections.get("MESH_DUMP", ""), ap, mac_to_ap, timestamp)
    points += parse_client_retries(sections.get("STATION_DUMP_phy1-ap0", ""), ap, "phy1-ap0", "2.4GHz", timestamp)
    points += parse_client_retries(sections.get("STATION_DUMP_phy2-ap0", ""), ap, "phy2-ap0", "5GHz", timestamp)
    points += parse_net_dev(sections.get("NET_DEV", ""), ap, timestamp)

    return points


def main():
    env = load_env()
    cfg = load_config()

    influx_token = env.get("INFLUXDB_ADMIN_TOKEN", "")
    if not influx_token:
        print("ERROR: INFLUXDB_ADMIN_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)

    timestamp = datetime.now(timezone.utc)
    ssh = cfg["ssh"]

    # Pass 1: SSH all APs, collect raw data and build mac -> ap_name lookup
    raw_by_ap = {}
    mac_to_ap = {}
    for ap in cfg["aps"]:
        print(f"Collecting {ap}...")
        try:
            raw = ssh_collect(f"{ap}.home.arpa", ssh["user"], ssh["key"], ssh["timeout"])
            sections = parse_sections(raw)
            raw_by_ap[ap] = sections
            self_mac = sections.get("SELF_MESH_MAC", "").strip()
            if self_mac:
                mac_to_ap[self_mac] = ap
        except Exception as e:
            print(f"ERROR: {ap}: SSH failed: {e}", file=sys.stderr)

    # Pass 2: Parse all collected data with the complete mac -> ap_name mapping
    all_points = []
    for ap, sections in raw_by_ap.items():
        points = parse_ap_data(ap, sections, mac_to_ap, timestamp)
        print(f"  {ap}: {len(points)} points")
        all_points += points

    if not all_points:
        print("ERROR: No data collected.", file=sys.stderr)
        sys.exit(1)

    influx_cfg = cfg["influxdb"]
    with InfluxDBClient(url=influx_cfg["url"], token=influx_token, org=influx_cfg["org"]) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=influx_cfg["bucket"], record=all_points)

    print(f"Wrote {len(all_points)} points to InfluxDB.")


if __name__ == "__main__":
    main()
