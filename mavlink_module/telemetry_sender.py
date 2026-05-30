#!/usr/bin/env python3
"""
Sirena Telemetry Sender

Auto-detects MAVLink flow locally — no server polling required.
  MAVLink flowing  → auto-starts sending to server
  No MAVLink 30s   → auto-stops (clears buffer)
  Data always accepted by server regardless of telemetry_active flag.
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error
from collections import deque

logger = logging.getLogger(__name__)

POLL_INTERVAL    = 5.0    # seconds between flow-detection checks
BATCH_INTERVAL   = 2.0    # seconds between telemetry batch sends
BATCH_MAX_SIZE   = 500    # max messages per HTTP POST
QUEUE_MAXLEN     = 5000   # in-memory buffer (~10s at 500 msg/s)
NO_DATA_TIMEOUT  = 30.0   # seconds without MAVLink before auto-stop


def mavmsg_to_dict(msg) -> dict:
    """Convert a pymavlink message to a plain Python dict."""
    try:
        result = {}
        for field in msg.get_fieldnames():
            v = getattr(msg, field, None)
            if v is None:
                continue
            if hasattr(v, 'item'):      # numpy scalar → Python native
                v = v.item()
            elif isinstance(v, (bytes, bytearray)):
                v = v.hex()
            elif isinstance(v, list):
                v = [x.item() if hasattr(x, 'item') else x for x in v]
            result[field] = v
        return result
    except Exception:
        return {}


class TelemetrySender:
    """
    Two background threads:
      - poll_worker:  every POLL_INTERVAL, checks local MAVLink flow
      - send_worker:  every BATCH_INTERVAL, flushes queue to server
    """

    def __init__(self, server_url: str, device_id: str, flight_id: str):
        self._server_url      = server_url.rstrip('/')
        self._device_id       = device_id
        self._flight_id       = flight_id
        self._queue: deque    = deque(maxlen=QUEUE_MAXLEN)
        self._active          = False
        self._running         = False
        self._sent_total      = 0
        self._last_send_ts    = 0.0
        self._last_enqueue_ts = 0.0   # updated on every incoming MAVLink msg

    # -----------------------------------------------------------------------
    def start(self):
        self._running = True
        threading.Thread(target=self._poll_worker, daemon=True, name="tel-poll").start()
        threading.Thread(target=self._send_worker, daemon=True, name="tel-send").start()
        logger.info(f"[Telemetry] Sender started (auto-mode) → {self._server_url}")

    def stop(self):
        self._running = False
        self._active  = False

    @property
    def is_active(self) -> bool:
        return self._active

    # -----------------------------------------------------------------------
    def enqueue(self, msg_type: str, fields: dict, ts: float):
        """Non-blocking. Called from mavlink_proxy_worker for every FC message."""
        self._last_enqueue_ts = ts   # always track flow, even before active
        if self._active:
            self._queue.append({"t": round(ts, 3), "m": msg_type, "d": fields})

    # -----------------------------------------------------------------------
    def _poll_worker(self):
        """Auto-detect MAVLink flow by watching _last_enqueue_ts."""
        while self._running:
            now      = time.time()
            has_flow = (self._last_enqueue_ts > 0 and
                        (now - self._last_enqueue_ts) < NO_DATA_TIMEOUT)

            if has_flow and not self._active:
                self._active = True
                logger.info("[Telemetry] ▶ MAVLink detected — auto-started streaming")

            elif not has_flow and self._active:
                self._active = False
                self._queue.clear()
                logger.info("[Telemetry] ⏹ No MAVLink for 30s — auto-stopped")

            time.sleep(POLL_INTERVAL)

    # -----------------------------------------------------------------------
    def _send_worker(self):
        while self._running:
            time.sleep(BATCH_INTERVAL)
            if not self._active or not self._queue:
                continue

            batch = []
            for _ in range(BATCH_MAX_SIZE):
                if not self._queue:
                    break
                batch.append(self._queue.popleft())

            if batch:
                self._send_batch(batch)

    def _send_batch(self, batch: list):
        payload = json.dumps({
            "device_id": self._device_id,
            "flight_id": self._flight_id,
            "msgs":      batch,
        }, separators=(',', ':')).encode()

        try:
            req = urllib.request.Request(
                f"{self._server_url}/api/telemetry",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                self._sent_total   += len(batch)
                self._last_send_ts  = time.time()
                if self._sent_total % 2000 == 0:
                    logger.info(f"[Telemetry] ✈ Sent {self._sent_total} messages total")
        except Exception as e:
            # Put messages back into the front of the queue (best-effort)
            for item in reversed(batch):
                try:
                    self._queue.appendleft(item)
                except Exception:
                    break
            logger.debug(f"[Telemetry] Send failed ({len(batch)} msgs re-queued): {e}")
