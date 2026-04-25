# Raspberry Pi 4 WM1302 MeshCore Stack (No RadioLib)

This stack avoids RadioLib for WM1302 by using:
- Semtech sx1302 packet forwarder (`lora_pkt_fwd`)
- A local bidirectional bridge (`bridge/meshcore_semtech_bridge.py`)
- MeshCore KISS protocol framing on the host side

## Why this design

MeshCore expects a bidirectional radio backend (RX + TX raw packet semantics). The bridge now speaks MeshCore-style KISS framing, not HTTP.

## Data paths

- RX path: `WM1302 -> lora_pkt_fwd (PUSH_DATA) -> bridge -> KISS DATA (0x00)`
- TX path: `KISS DATA (0x00) -> bridge -> lora_pkt_fwd (PULL_RESP/txpk) -> WM1302`

Optional metadata frames are sent as KISS SetHardware `RX_META (0xF9)`.

## Important note about frequency plan

Use the `global_conf.*.json` template that matches your legal region and your node parameters. If MeshCore-side packet parameters and gateway/channel config do not match, packets will not decode.

## Install on Pi 4

Run from this folder on the Pi:

```bash
sudo bash install_pi4.sh
```

Then:

1. Pick region config:
```bash
sudo cp /opt/meshcore-wm1302/config/global_conf.EU868.json /opt/meshcore-wm1302/config/global_conf.json
```

2. Edit bridge env:
```bash
sudo nano /etc/default/meshcore-semtech-bridge
```

3. Start services:
```bash
sudo systemctl restart meshcore-semtech-bridge wm1302-pkt-fwd
sudo systemctl status meshcore-semtech-bridge wm1302-pkt-fwd
```

## KISS endpoint

By default the bridge listens on:
- `127.0.0.1:8001`

Connect your MeshCore-side host/client to this endpoint using KISS framing.

## Service logs

```bash
sudo journalctl -u meshcore-semtech-bridge -f
sudo journalctl -u wm1302-pkt-fwd -f
```

## Configuration files

- Bridge env: `/etc/default/meshcore-semtech-bridge`
- Bridge service: `/etc/systemd/system/meshcore-semtech-bridge.service`
- Packet forwarder service: `/etc/systemd/system/wm1302-pkt-fwd.service`
- Packet forwarder config: `/opt/meshcore-wm1302/config/global_conf.json`

## Current limits

- One active KISS TCP client at a time.
- Bridge emits immediate TX (`imme=true`) via Semtech `txpk`.
- TX success notification indicates PULL_RESP submission to packet forwarder; not RF-level delivery guarantee.
