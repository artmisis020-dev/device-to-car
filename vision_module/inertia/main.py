"""Логування IMU/attitude телеметрії дрона через MAVLink у CSV для подальшого
офлайн-розрахунку траєкторії (див. replay.py, estimator.py).

Об'єднує main.py (базове логування) і main_work.py (перемикач логування
через 7-й канал RC) в один клас. Відеооверлей через framebuffer прибрано —
не стосується інерційки. Логування PWM моторів (SERVO_OUTPUT_RAW) закоментовано:
цей код використовується і для літаків, і для коптерів, а моторна телеметрія
релевантна лише для мультироторів — за потреби розкоментувати нижче.
"""
from pymavlink import mavutil
import time
import csv
import math

HEADERS = [
    'timestamp',
    'acc_x', 'acc_y', 'acc_z',
    'gyro_x', 'gyro_y', 'gyro_z',
    'roll', 'pitch', 'yaw',
    'rollspeed', 'pitchspeed', 'yawspeed',
    'baro_alt', 'pressure',
    'system_time_us', 'current_time',
    'local_x', 'local_y', 'local_z',
    'local_vx', 'local_vy', 'local_vz',
    'highres_acc_x', 'highres_acc_y', 'highres_acc_z',
    'highres_gyro_x', 'highres_gyro_y', 'highres_gyro_z',
    'highres_pressure', 'highres_temperature',
    'highres_timestamp',
    'scaled_acc_x', 'scaled_acc_y', 'scaled_acc_z',
    'scaled_gyro_x', 'scaled_gyro_y', 'scaled_gyro_z',
    'scaled_temperature',
    # GPS_RAW_INT / EKF_STATUS_REPORT — потрібні для gps_integrity.py (виявлення
    # джемінгу/спуфінгу): fix_type і сузір'я падають при джемінгу; варіанс EKF
    # лишається "впевненим" навіть коли позиція фізично неможлива — ознака спуфінгу.
    'gps_fix_type', 'gps_satellites_visible', 'gps_eph', 'gps_epv', 'gps_vel_cms',
    'gps_lat', 'gps_lon',
    'ekf_pos_horiz_variance', 'ekf_velocity_variance', 'ekf_flags',
    # MAV_TYPE з HEARTBEAT (1=fixed_wing, 2=quadrotor, ...) — визначає
    # дефолтні ZUPT/NHC/airspeed у ekf_replay.py (див. airframe.py):
    # мультиротор і літак з фіксованим крилом потребують ПРОТИЛЕЖних
    # налаштувань цих корекцій.
    'mav_type',
    # 'motor1_pwm', 'motor2_pwm', 'motor3_pwm', 'motor4_pwm',
    # 'motor5_pwm', 'motor6_pwm', 'motor7_pwm', 'motor8_pwm',
]


def apply_mavlink_message(last_data, msg, current_time):
    """Оновлює словник last_data одним MAVLink-повідомленням.

    Спільна для живого логування (DroneLogger, серійний зв'язок) і офлайн-конвертації
    записаних .tlog/.rlog (tlog_to_csv.py) — щоб парсинг повідомлень не дублювався.
    """
    msg_type = msg.get_type()

    if msg_type == 'RAW_IMU':
        last_data.update({
            'acc_x': msg.xacc, 'acc_y': msg.yacc, 'acc_z': msg.zacc,
            'gyro_x': msg.xgyro, 'gyro_y': msg.ygyro, 'gyro_z': msg.zgyro,
        })
    elif msg_type == 'ATTITUDE':
        last_data.update({
            'roll': math.degrees(msg.roll),
            'pitch': math.degrees(msg.pitch),
            'yaw': math.degrees(msg.yaw),
            'rollspeed': msg.rollspeed,
            'pitchspeed': msg.pitchspeed,
            'yawspeed': msg.yawspeed,
        })
    elif msg_type == 'LOCAL_POSITION_NED':
        last_data.update({
            'local_x': msg.x, 'local_y': msg.y, 'local_z': msg.z,
            'local_vx': msg.vx, 'local_vy': msg.vy, 'local_vz': msg.vz,
        })
    elif msg_type == 'HIGHRES_IMU':
        last_data.update({
            'highres_acc_x': msg.xacc, 'highres_acc_y': msg.yacc, 'highres_acc_z': msg.zacc,
            'highres_gyro_x': msg.xgyro, 'highres_gyro_y': msg.ygyro, 'highres_gyro_z': msg.zgyro,
            'highres_pressure': msg.abs_pressure,
            'highres_temperature': msg.temperature,
            'highres_timestamp': msg.time_usec,
        })
    elif msg_type == 'SCALED_IMU':
        last_data.update({
            'scaled_acc_x': msg.xacc / 1000.0,
            'scaled_acc_y': msg.yacc / 1000.0,
            'scaled_acc_z': msg.zacc / 1000.0,
            'scaled_gyro_x': msg.xgyro / 1000.0,
            'scaled_gyro_y': msg.ygyro / 1000.0,
            'scaled_gyro_z': msg.zgyro / 1000.0,
            'scaled_temperature': msg.temperature / 100.0,
        })
    elif msg_type == 'GLOBAL_POSITION_INT':
        last_data.update({
            'baro_alt': msg.relative_alt / 1000.0,
        })
    elif msg_type == 'SCALED_PRESSURE':
        last_data.update({'pressure': msg.press_abs})
    elif msg_type == 'GPS_RAW_INT':
        # lat/lon — з самого приймача (є завжди, коли є фікс), незалежно від того,
        # чи автопілот публікує LOCAL_POSITION_NED/GLOBAL_POSITION_INT.lat-lon
        # (на деяких бортах/прошивках ці поля лишаються нульовими без заданого
        # EKF origin, хоча GPS-приймач сам по собі впевнено тримає фікс).
        last_data.update({
            'gps_fix_type': msg.fix_type,
            'gps_satellites_visible': msg.satellites_visible,
            'gps_eph': msg.eph,
            'gps_epv': msg.epv,
            'gps_vel_cms': msg.vel,
            'gps_lat': msg.lat,
            'gps_lon': msg.lon,
        })
    elif msg_type == 'EKF_STATUS_REPORT':
        last_data.update({
            'ekf_pos_horiz_variance': msg.pos_horiz_variance,
            'ekf_velocity_variance': msg.velocity_variance,
            'ekf_flags': msg.flags,
        })
    elif msg_type == 'SYSTEM_TIME':
        last_data.update({
            'system_time_us': msg.time_unix_usec,
            'current_time': current_time,
        })
    elif msg_type == 'HEARTBEAT':
        # MAV_TYPE_GCS(6)/MAV_AUTOPILOT_INVALID(8) — heartbeat наземної
        # станції чи іншого не-апарата на тому ж лінку, а не самого дрона;
        # той самий фільтр, що вже є в admin_module OSD (_video_player.html) —
        # інакше mav_type може випадково перезаписатись чужим значенням.
        if msg.type != 6 and msg.autopilot != 8:
            last_data.update({'mav_type': msg.type})
    # elif msg_type == 'SERVO_OUTPUT_RAW':
    #     last_data.update({
    #         'motor1_pwm': msg.servo1_raw, 'motor2_pwm': msg.servo2_raw,
    #         'motor3_pwm': msg.servo3_raw, 'motor4_pwm': msg.servo4_raw,
    #         'motor5_pwm': msg.servo5_raw, 'motor6_pwm': msg.servo6_raw,
    #         'motor7_pwm': msg.servo7_raw, 'motor8_pwm': msg.servo8_raw,
    #     })
    return msg_type


class DroneLogger:
    def __init__(self, port='/dev/ttyS0', baud=115200, rc_channel_gate=7, write_interval=0.1):
        self.port = port
        self.baud = baud
        self.rc_channel_gate = rc_channel_gate
        self.write_interval = write_interval
        self.mav = None
        self.logging_enabled = False

    def setup_connection(self):
        try:
            self.mav = mavutil.mavlink_connection(self.port, self.baud)
            self.mav.wait_heartbeat()
            print(f"З'єднання встановлено з системою {self.mav.target_system}")
            self.mav.mav.request_data_stream_send(
                self.mav.target_system, self.mav.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL, 50, 1)
            return True
        except Exception as e:
            print(f"Помилка підключення: {e}")
            return False

    def check_rc_channels(self):
        """Перевірка гейт-каналу RC (за замовчуванням CH7): >1500 -> логування увімкнено."""
        msg = self.mav.recv_match(type='RC_CHANNELS', blocking=False)
        if msg is None:
            return
        chan_raw = getattr(msg, f'chan{self.rc_channel_gate}_raw', None)
        if chan_raw is None:
            return
        should_log = chan_raw > 1500
        if should_log != self.logging_enabled:
            self.logging_enabled = should_log
            print(f"Логування {'увімкнено' if should_log else 'вимкнено'} (CH{self.rc_channel_gate}={chan_raw})")

    @staticmethod
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
            'mav_type': 0,  # 0 = MAV_TYPE_GENERIC ("не визначено", див. airframe.py)
            # 'motor1_pwm': 0, 'motor2_pwm': 0, 'motor3_pwm': 0, 'motor4_pwm': 0,
            # 'motor5_pwm': 0, 'motor6_pwm': 0, 'motor7_pwm': 0, 'motor8_pwm': 0,
        }

    def log_flight_data(self, output_path='flight_logs.csv', require_rc_gate=True, duration=None):
        print("Очікування активації логування..." if require_rc_gate else "Початок логування...")
        start_time = time.time()
        last_data = self._default_data()
        received_msg_count = 0
        written_rows_count = 0

        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(HEADERS)
            last_write_time = time.time()

            try:
                while True:
                    if duration and (time.time() - start_time) > duration:
                        break

                    if require_rc_gate:
                        self.check_rc_channels()

                    if not require_rc_gate or self.logging_enabled:
                        msg = self.mav.recv_match(blocking=False)
                        current_time = time.time()

                        if msg is not None:
                            apply_mavlink_message(last_data, msg, current_time)
                            received_msg_count += 1

                        if current_time - last_write_time >= self.write_interval:
                            row = [current_time] + [last_data.get(h, 0.0) for h in HEADERS[1:]]
                            writer.writerow(row)
                            written_rows_count += 1

                            if written_rows_count % 10 == 0:
                                print(
                                    f"Записано рядків: {written_rows_count} | "
                                    f"Отримано повідомлень: {received_msg_count} | "
                                    f"RPY: {last_data['roll']:.2f} {last_data['pitch']:.2f} {last_data['yaw']:.2f}"
                                )
                            last_write_time = current_time
                    else:
                        time.sleep(0.01)

            except KeyboardInterrupt:
                print("\nЛогування зупинено користувачем")
                print(f"Всього записано {written_rows_count} рядків")


def main():
    logger = DroneLogger()
    if logger.setup_connection():
        # require_rc_gate=False відтворює стару поведінку main.py (лог одразу після конекту);
        # require_rc_gate=True — поведінка main_work.py (лог по 7-му каналу).
        logger.log_flight_data(require_rc_gate=True)
    else:
        print("Помилка ініціалізації логера")


if __name__ == "__main__":
    main()
