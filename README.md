# Sirena

Sirena is a Raspberry Pi drone stack split into local workers and a remote admin server.

On the RPi, the local manager starts and supervises:
- `mavlink_router` for FC/GCS telemetry routing
- `telemetry_sender` for sending MAVLink telemetry to the admin server
- `navigation` for GPS/Starlink/Beitian handling
- `video_manager` and `video_relay` for camera streaming

The remote admin server stores registered drones, telemetry, video state, and the drone detail page.

## Start on RPi

Fast path:

```bash
sudo bash install_rpi.sh http://<admin-server-ip>:8080
```

That script copies the RPi-side code to `/opt/sirena`, installs the local worker modules, writes the Raspberry Pi boot UART config, creates the root venv, writes `/opt/sirena/.env`, and enables `sirena-manager.service`.

UART mapping used by the installer:
- `uart2` for Beitian GPS
- `uart3` for FC GPS/NMEA output
- `uart5` for FC telemetry on GPIO12/13 (pins 32/33)

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