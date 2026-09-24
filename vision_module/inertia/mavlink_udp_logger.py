"""Пасивний логер MAVLink з UDP → CSV того самого формату, що main.py/
tlog_to_csv.py (HEADERS/apply_mavlink_message — без дублювання).

Призначення: живий тап повного MAVLink-потоку з дрона на Spark. Джерело —
не пряме з'єднання з РПі (RPi і Spark не бачать одне одного напряму по
WireGuard — лише через хаб, адмін-сервер), а UDP-реле на адмін-сервері,
яке пересилає сюди все, що RPi's mavlink-router шле на порт 14570 хабу
(див. README.md, розділ "Живе надходження MAVLink на Spark").

Канал ОДНОСТОРОННІЙ (socat-реле лише RECV→SENDTO) — тому на відміну від
main.py::DroneLogger жодних запитів потоку (request_data_stream_send) назад
не шлемо: нема кому їх отримати, і не потрібно — mavlink-router і так
розсилає РІВНО те, що вже тече з FC, без додаткових запитів з нашого боку.

Використання:
    python3 mavlink_udp_logger.py --listen udpin:0.0.0.0:14570 --out live.csv
"""
from __future__ import annotations

import argparse
import csv
import time

from pymavlink import mavutil

from main import HEADERS, DroneLogger, apply_mavlink_message


def run(listen_url: str, output_path: str, write_interval: float = 0.1, duration: float | None = None):
    print(f"Очікування MAVLink на {listen_url}...")
    mav = mavutil.mavlink_connection(listen_url)
    mav.wait_heartbeat()
    print(f"З'єднання встановлено, система {mav.target_system}:{mav.target_component}")

    last_data = DroneLogger._default_data()
    start_time = time.time()
    received = written = 0

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        last_write_time = time.time()

        while True:
            if duration and (time.time() - start_time) > duration:
                break

            # blocking з таймаутом — не крутимо CPU в холосту між пакетами,
            # але й не застрягаємо назавжди, якщо потік тимчасово зник
            # (дає можливості дописати рядок останнім відомим станом,
            # а не просто зависнути на recv_match).
            msg = mav.recv_match(blocking=True, timeout=1.0)
            current_time = time.time()

            if msg is not None and msg.get_type() not in ("BAD_DATA", None):
                apply_mavlink_message(last_data, msg, current_time)
                received += 1

            if current_time - last_write_time >= write_interval:
                row = [current_time] + [last_data.get(h, 0.0) for h in HEADERS[1:]]
                writer.writerow(row)
                written += 1
                if written % 50 == 0:
                    print(f"Записано {written} рядків | отримано {received} повідомлень")
                last_write_time = current_time

    print(f"Готово: {written} рядків у {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="udpin:0.0.0.0:14570", help="pymavlink connection string")
    parser.add_argument("--out", default="flight_log_live.csv")
    parser.add_argument("--interval", type=float, default=0.1, help="Інтервал запису рядків CSV, с")
    parser.add_argument("--duration", type=float, default=None, help="Зупинитись через N секунд (за замовчуванням — безкінечно)")
    args = parser.parse_args()

    run(args.listen, args.out, write_interval=args.interval, duration=args.duration)
