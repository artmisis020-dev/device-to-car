"""Конвертує ArduPilot dataflash-лог (.bin, з SD-карти польотного контролера)
у CSV того самого формату, що tlog_to_csv.py — щоб прогнати офлайн через
replay.py/vizualization.py/route_summary.py/gps_integrity.py без жодних змін
у цих файлах.

Dataflash-лог набагато повніший і надійніший за телеметрійний .tlog (пишеться
локально на борту, не залежить від радіоканалу) — і має пряму EKF-позицію
(XKF1.PN/PE/PD) та сирий GPS (GPS.Lat/Lng/Spd) окремими джерелами.

Одиниці тут ІНШІ, ніж у MAVLink RAW_IMU/GPS_RAW_INT:
  - IMU.AccX/Y/Z вже в м/с² (не мГ) — конвертуємо назад у мГ, щоб той самий
    imu_math.mg_to_ms2() у replay.py відпрацював без змін.
  - IMU.GyrX/Y/Z вже в рад/с (не мрад/с) — аналогічно конвертуємо в мрад/с.
  - GPS.Lat/Lng вже в градусах (не degE7) — множимо на 1e7.
  - GPS.Spd вже в м/с — множимо на 100 (см/с), як gps_vel_cms.
  - BARO.Alt — висота відносно точки старту (м, вгору-додатна) — той самий
    сенс, що baro_alt в основному CSV-форматі.

Використання:
    python3 dataflash_to_csv.py шлях/до/логу.bin [--out файл.csv] [--rate 10]
"""
from __future__ import annotations

import argparse
import csv

from pymavlink import mavutil

from main import HEADERS

GRAVITY = 9.80665


def _default_data():
    return {
        'acc_x': 0.0, 'acc_y': 0.0, 'acc_z': 0.0,
        'gyro_x': 0.0, 'gyro_y': 0.0, 'gyro_z': 0.0,
        'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
        'rollspeed': 0.0, 'pitchspeed': 0.0, 'yawspeed': 0.0,
        'baro_alt': 0.0, 'pressure': 0.0,
        'system_time_us': 0, 'current_time': 0,
        'local_x': 0.0, 'local_y': 0.0, 'local_z': 0.0,
        'local_vx': 0.0, 'local_vy': 0.0, 'local_vz': 0.0,
        'highres_acc_x': 0.0, 'highres_acc_y': 0.0, 'highres_acc_z': 0.0,
        'highres_gyro_x': 0.0, 'highres_gyro_y': 0.0, 'highres_gyro_z': 0.0,
        'highres_pressure': 0.0, 'highres_temperature': 0.0,
        'highres_timestamp': 0,
        'scaled_acc_x': 0.0, 'scaled_acc_y': 0.0, 'scaled_acc_z': 0.0,
        'scaled_gyro_x': 0.0, 'scaled_gyro_y': 0.0, 'scaled_gyro_z': 0.0,
        'scaled_temperature': 0.0,
        'gps_fix_type': 0, 'gps_satellites_visible': 0, 'gps_eph': 9999, 'gps_epv': 9999, 'gps_vel_cms': 0,
        'gps_lat': 0, 'gps_lon': 0,
        'ekf_pos_horiz_variance': 0.0, 'ekf_velocity_variance': 0.0, 'ekf_flags': 0,
        # EKF3-позиція (XKF1.PN/PE/PD) — для порівняння з нашим ІНШИМ, незалежним
        # інерційним розрахунком; не використовується replay.py напряму (там свій
        # local_x/y/z шлях), пишемо в окремі колонки для довідки/аналізу.
        'ekf3_pn': 0.0, 'ekf3_pe': 0.0, 'ekf3_pd': 0.0,
        # Піто-трубка (лише літаки з фіксованим крилом) — незалежне від GPS
        # джерело швидкості, не дрейфує з часом (див. ekf_estimator.update_airspeed).
        'airspeed_ms': 0.0,
    }


def convert(bin_path, output_csv, write_interval=0.1, sync_to_imu=True):
    """write_interval — мінімальний крок часу між рядками CSV.

    sync_to_imu=True (за замовчуванням): рядок пишеться на КОЖНОМУ семплі
    IMU (I=0), а write_interval лише відсіює надто часті повтори. Це
    критично для точності інтегрування — dataflash IMU йде на ~150Гц,
    а old-style "знімок раз на 100мс" (сумісний з MAVLink-темпом tlog)
    тримав "останнє відоме" значення акселерометра і губив ~93% реальних
    семплів саме в динамічній фазі (зліт/маневр), де вони найважливіші.
    sync_to_imu=False відтворює стару поведінку (для сумісності/дебагу).
    """
    mlog = mavutil.mavlink_connection(bin_path)
    last_data = _default_data()
    extra_headers = ['ekf3_pn', 'ekf3_pe', 'ekf3_pd', 'airspeed_ms']
    headers = HEADERS + extra_headers

    last_write_time = None
    written = 0
    n_msgs = 0
    start_t = end_t = None

    with open(output_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        while True:
            msg = mlog.recv_match(blocking=False)
            if msg is None:
                break
            n_msgs += 1
            msg_type = msg.get_type()
            if msg_type in ('FMT', 'FMTU', 'UNIT', 'MULT', 'PARM'):
                continue

            time_us = getattr(msg, 'TimeUS', None)
            if time_us is None:
                continue
            t = time_us / 1e6  # секунди від завантаження — досить для коректних dt
            if start_t is None:
                start_t = t
            end_t = t

            is_primary_imu = msg_type == 'IMU' and getattr(msg, 'I', 0) == 0
            if is_primary_imu:
                last_data.update({
                    'acc_x': msg.AccX * 1000.0 / GRAVITY,
                    'acc_y': msg.AccY * 1000.0 / GRAVITY,
                    'acc_z': msg.AccZ * 1000.0 / GRAVITY,
                    'gyro_x': msg.GyrX * 1000.0,
                    'gyro_y': msg.GyrY * 1000.0,
                    'gyro_z': msg.GyrZ * 1000.0,
                })
            elif msg_type == 'ATT':
                last_data.update({
                    'roll': msg.Roll, 'pitch': msg.Pitch, 'yaw': msg.Yaw,
                })
            elif msg_type == 'BARO' and getattr(msg, 'I', 0) == 0:
                last_data.update({'baro_alt': msg.Alt, 'pressure': msg.Press})
            elif msg_type == 'GPS' and getattr(msg, 'I', 0) == 0:
                last_data.update({
                    'gps_fix_type': msg.Status,
                    'gps_satellites_visible': msg.NSats,
                    'gps_eph': msg.HDop * 100.0,
                    'gps_vel_cms': msg.Spd * 100.0,
                    'gps_lat': msg.Lat * 1e7,
                    'gps_lon': msg.Lng * 1e7,
                })
            elif msg_type == 'XKF1' and getattr(msg, 'C', 0) == 0:
                last_data.update({
                    'ekf3_pn': msg.PN, 'ekf3_pe': msg.PE, 'ekf3_pd': msg.PD,
                })
            elif msg_type == 'ARSP' and getattr(msg, 'I', 0) == 0:
                last_data.update({'airspeed_ms': msg.Airspeed})

            write_trigger = is_primary_imu if sync_to_imu else True
            if write_trigger and (last_write_time is None or (t - last_write_time) >= write_interval):
                row = [t] + [last_data.get(h, 0.0) for h in HEADERS[1:]] + [last_data[h] for h in extra_headers]
                writer.writerow(row)
                written += 1
                last_write_time = t

    duration = (end_t - start_t) if (start_t is not None and end_t is not None) else 0.0
    return {'rows_written': written, 'duration_s': duration, 'messages_read': n_msgs}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bin_path')
    parser.add_argument('--out', default=None)
    parser.add_argument('--rate', type=float, default=200.0, help='Макс. частота запису рядків, Гц (за замовч. 200 — вище за нативну ~150Гц IMU, тобто без прорідження)')
    parser.add_argument('--no-imu-sync', action='store_true', help='Стара поведінка: знімок за годинником, а не на кожному семплі IMU (менш точно, але менший файл)')
    args = parser.parse_args()

    out_path = args.out or (args.bin_path.rsplit('.', 1)[0] + '.csv')
    result = convert(args.bin_path, out_path, write_interval=1.0 / args.rate, sync_to_imu=not args.no_imu_sync)
    print(f"Прочитано {result['messages_read']} повідомлень з {args.bin_path}")
    print(f"Записано {result['rows_written']} рядків у {out_path}")
    print(f"Тривалість логу: {result['duration_s']:.1f} с")
