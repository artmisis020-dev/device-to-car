#!/usr/bin/env python3
"""
Sirena CRSF Daemon
Standalone обгортка: піднімає CRSFBridge, який приймає RC-канали по UDP
і транслює їх у польотний контролер через UART (протокол CRSF).

Сервіс: crsf-bridge.service
"""

import logging
import signal
import sys
import time

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [crsf-daemon]: %(message)s",
)
logger = logging.getLogger(__name__)

CONNECT_RETRY = 10  # секунд між спробами підняти міст


def main() -> None:
    try:
        from crsf_bridge import CRSFBridge
    except ImportError:
        logger.error("crsf_bridge module not found")
        sys.exit(1)

    bridge = CRSFBridge()

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received")
        bridge.stop()
        bridge.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "CRSF Daemon started | UART=%s@%d | UDP=%s:%d",
        config.CRSF_UART_PORT, config.CRSF_UART_BAUD,
        config.CRSF_BIND_HOST, config.CRSF_BIND_PORT,
    )

    while True:
        try:
            bridge.connect()
            bridge.run()
        except Exception as exc:
            bridge.disconnect()
            logger.warning("CRSF bridge error: %s — retrying in %ds", exc, CONNECT_RETRY)
            time.sleep(CONNECT_RETRY)


if __name__ == "__main__":
    main()
