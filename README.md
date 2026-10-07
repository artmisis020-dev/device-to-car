# Sirena

Sirena is a Raspberry Pi drone stack split into local workers and a remote admin server.

On the RPi, the local manager starts and supervises:
- `mavlink_router` for FC/GCS telemetry routing
- `telemetry_sender` for sending MAVLink telemetry to the admin server
- `navigation` for GPS/Starlink/Beitian handling
- `video_manager` and `video_relay` for camera streaming

Independently of the manager boot sequence, `mesh_module` brings up an encrypted
802.11s mesh between boards of one mesh group (configured in the admin UI) on a
separate USB Wi-Fi adapter: if a board's Starlink dies, its WireGuard goes
through a neighbour's Starlink (~1s switchover).

The remote admin server stores registered drones, telemetry, video state, and the drone detail page.

## Video

Camera → SRT → MediaMTX → WebRTC with ~50-70ms glass-to-glass latency and a
fast adaptive bitrate. Architecture, measured latency budget, the MediaMTX
patches, adaptive bitrate design and settings: [`video_module/README.md`](video_module/README.md).

The admin server must run the **patched** MediaMTX build from
`admin_module/deploy/mediamtx-patches/` (installed automatically by
`deploy.sh`); the stock release adds ~90ms of latency.

## Mesh (Starlink failover between boards)

Design, failover logic, tuning, bench test results and known hardware issues:
[`mesh_module/README.md`](mesh_module/README.md). Installed by `install_rpi.sh`;
admin UI — `/mesh/groups` (mesh groups: which boards back each other up;
the board's `sirena-mesh-agent` pulls the group config) and `/mesh`.
Release notes: [`CHANGELOG.md`](CHANGELOG.md).

## Deploy Admin Server

On the admin host, place the project under `/opt/sirena-admin` and run:

```bash
bash /opt/sirena-admin/admin_module/deploy/deploy.sh
```

This deploy script now applies both services:
- `sirena-admin` (Gunicorn/Flask app)
- `mediamtx-admin` (patched low-latency MediaMTX from `admin_module/deploy/mediamtx-patches/`, config `admin_module/mediamtx.yml`)

If you only need to re-apply MediaMTX:

```bash
sudo bash /opt/sirena-admin/admin_module/deploy/install_mediamtx.sh \
	/opt/sirena-admin/admin_module/mediamtx.yml \
	mediamtx-admin
```

## Start on RPi

Fast path:

```bash
sudo bash install_rpi.sh http://<admin-server-ip>:8080
```

That script copies the RPi-side code to `/opt/sirena`, installs the local worker modules, writes the Raspberry Pi boot UART config, creates the root venv, writes `/opt/sirena/.env`, and enables `sirena-manager.service`.

UART mapping used by the installer:
- `uart2` for Beitian GPS
- `uart3` for FC GPS/NMEA output
- `uart0` for FC MAVLink telemetry on GPIO14/15 (pins 8/10)

Manual start:

```bash
SIRENA_ADMIN_SERVER_URL=http://<admin-server-host>:8080 python main.py
```

If you run the manager as a systemd service, use the `sirena-manager.service` unit from `sirena_manager/deploy/`.
Set `SIRENA_ADMIN_SERVER_URL` in `/opt/sirena/.env` or in the service environment so the RPi can register with the remote admin server.

## Useful endpoints

- `GET /api/v1/health` on the root manager
- `GET /api/devices/<device_id>` on the admin server for a drone detail payload
- `GET /devices/<device_id>` on the admin UI for the full drone detail page
