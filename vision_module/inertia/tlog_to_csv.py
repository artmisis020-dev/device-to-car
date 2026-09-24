"""Конвертує записаний MAVLink .tlog (Mission Planner / MAVProxy / QGroundControl)
у CSV того самого формату, що пише main.py — щоб прогнати політ офлайн через
replay.py / vizualization.py / ml_experiment.py.

.tlog — це той самий потік MAVLink-повідомлень, який DroneLogger читає наживо
з серійного порту, тільки записаний у файл з 8-байтовим таймстемпом перед
кожним повідомленням; pymavlink вміє відтворювати його так само, як і живе
з'єднання, тому диспетчеризація повідомлень (apply_mavlink_message) —
та сама, що й у main.py, без дублювання.

Використання:
    python3 tlog_to_csv.py шлях/до/файлу.tlog [--out flight_logs_from_tlog.csv] [--rate 10]
"""
from __future__ import annotations

import argparse
import csv

from pymavlink import mavutil

from main import HEADERS, DroneLogger, apply_mavlink_message


def convert(tlog_path, output_csv, write_interval=0.1):
    mav = mavutil.mavlink_connection(tlog_path)
    last_data = DroneLogger._default_data()

    last_write_time = None
    written = 0
    seen_types = set()
    start_t = end_t = None

    with open(output_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)

        while True:
            msg = mav.recv_match(blocking=False)
            if msg is None:
                break

            msg_type = msg.get_type()
            if msg_type in ('BAD_DATA', None):
                continue

            t = getattr(msg, '_timestamp', None)
            if t is None:
                continue

            if start_t is None:
                start_t = t
            end_t = t
            seen_types.add(msg_type)

            apply_mavlink_message(last_data, msg, t)

            if last_write_time is None or (t - last_write_time) >= write_interval:
                row = [t] + [last_data.get(h, 0.0) for h in HEADERS[1:]]
                writer.writerow(row)
                written += 1
                last_write_time = t

    duration = (end_t - start_t) if (start_t is not None and end_t is not None) else 0.0
    return {
        'rows_written': written,
        'duration_s': duration,
        'message_types': sorted(seen_types),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tlog_path')
    parser.add_argument('--out', default=None, help='Куди зберегти CSV (за замовчуванням <ім\'я>.csv поруч)')
    parser.add_argument('--rate', type=float, default=10.0, help='Частота запису рядків CSV, Гц (за замовчуванням 10)')
    args = parser.parse_args()

    out_path = args.out or (args.tlog_path.rsplit('.', 1)[0] + '.csv')
    result = convert(args.tlog_path, out_path, write_interval=1.0 / args.rate)

    print(f"Прочитано з {args.tlog_path}")
    print(f"Записано {result['rows_written']} рядків у {out_path}")
    print(f"Тривалість логу: {result['duration_s']:.1f} с")
    print(f"Типи MAVLink-повідомлень у файлі: {', '.join(result['message_types'])}")
