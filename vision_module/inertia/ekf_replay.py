"""Офлайн-прогін EKFEstimator (ekf_estimator.py) через записаний лог,
з періодичною симуляцією GPS/visual-фіксів — аналог replay.py, але для
Kalman-фільтра замість порогового InertialEstimator.

Навіщо періодична симуляція: чиста unaided-інерційка розходиться на довгих
логах (задокументовано в replay.py). Реальний сценарій використання —
доповнення visual-навігації: EKF тримає позицію МІЖ послідовними
візуальними/GPS-фіксами, а не замінює їх на весь політ. reset_interval_s
імітує, як часто такий фікс надходив би.

Використання:
    python3 ekf_replay.py лог.csv [--interval 10] [--pos-std 0.5] [--vel-std 0.2]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

import airframe
from ekf_estimator import EKFConfig, EKFEstimator
from imu_math import deg_to_rad, mg_to_ms2, rotation_matrix
from replay import _gps_source, _latlon_to_north_east, GPS_MAX_JUMP_DISTANCE_M, load_log

sys.path.insert(0, str(Path(__file__).resolve().parent / "optical_flow"))
from ekf_bridge import update_ekf_with_flow  # noqa: E402
from flow_estimator import OpticalFlowEstimator  # noqa: E402
from video_sync import RecordingFrameSource  # noqa: E402


def _build_gps_reference(rows):
    """Той самий GPS-еталон (з origin-reset стичкою), що й у replay.run()."""
    gps_source = _gps_source(rows)
    n = len(rows)
    if gps_source is None:
        return None
    gps_positions = np.zeros((n, 3))
    gps_origin_shift = np.zeros(3)
    last_valid_gps = None
    latlon_origin = None
    for i, row in enumerate(rows):
        if gps_source == "local_position":
            lx = float(row.get("local_x") or 0.0)
            ly = float(row.get("local_y") or 0.0)
            lz = float(row.get("local_z") or 0.0)
            raw = np.array([lx, ly, -lz])
        else:
            lat = float(row.get("gps_lat") or 0) / 1e7
            lon = float(row.get("gps_lon") or 0) / 1e7
            if lat != 0.0 or lon != 0.0:
                if latlon_origin is None:
                    latlon_origin = (lat, lon)
                north, east = _latlon_to_north_east(lat, lon, *latlon_origin)
            else:
                north, east = (last_valid_gps[0], last_valid_gps[1]) if last_valid_gps is not None else (0.0, 0.0)
            z = float(row.get("baro_alt") or 0.0)
            raw = np.array([north, east, z])
        corrected = raw - gps_origin_shift
        if last_valid_gps is not None:
            jump = np.linalg.norm(corrected - last_valid_gps)
            if jump > GPS_MAX_JUMP_DISTANCE_M:
                gps_origin_shift = gps_origin_shift + (corrected - last_valid_gps)
                corrected = raw - gps_origin_shift
        gps_positions[i] = corrected
        last_valid_gps = corrected
    return gps_positions


def _gps_velocity_at(gps_positions, t_all, i, win_s=2.0):
    n = len(t_all)
    lo = i
    while lo > 0 and t_all[i] - t_all[lo] < win_s / 2:
        lo -= 1
    hi = i
    while hi < n - 1 and t_all[hi] - t_all[i] < win_s / 2:
        hi += 1
    dt = t_all[hi] - t_all[lo]
    if dt <= 0:
        return np.zeros(3)
    return (gps_positions[hi] - gps_positions[lo]) / dt


def _estimate_wind(gps_positions, t_all, i, roll, pitch, yaw, airspeed_ms, win_s=2.0):
    """Вітер = справжня GPS-швидкість мінус вектор повітряної швидкості
    (airspeed вперед по корпусу). Рахується заново при кожному фіксі —
    вітер не сталий, але змінюється повільніше, ніж потрібна корекція
    траєкторії, тому "заморожений" між фіксами вітер — прийнятне наближення."""
    true_v = _gps_velocity_at(gps_positions, t_all, i, win_s)
    R = rotation_matrix(roll, pitch, yaw)
    R_enu = R.copy(); R_enu[2, :] = -R_enu[2, :]
    airspeed_enu = R_enu @ np.array([airspeed_ms, 0.0, 0.0])
    return true_v - airspeed_enu


def run(csv_path, reset_interval_s=10.0, pos_std=0.5, vel_std=0.2,
        use_baro=True, use_zupt=None, use_nhc=None, use_airspeed=None,
        video_path=None, use_flow=None,
        config: EKFConfig | None = None, start_idx=0, max_dt=0.5, verbose=True):
    """Прогонити лог через EKFEstimator з періодичними псевдо-GPS/visual
    корекціями (reset_interval_s) — щоб виміряти, наскільки далеко "уносить"
    траєкторію МІЖ фіксами (а не за весь лог одразу, як у replay.run()).

    use_zupt/use_nhc/use_airspeed: None (за замовчуванням) = АВТО — тип
    апарата визначається з колонки mav_type (MAVLink HEARTBEAT.type,
    записується main.py/mavlink_udp_logger.py/tlog_to_csv.py) і застосовуються
    дефолти з airframe.default_flags(). Явний True/False завжди перекриває
    авто-визначення. Це принципово, бо:
      - ZUPT коректний для мультиротора (реально зависає нерухомо), але
        хибно спрацьовує на літаку з фіксованим крилом у рівному польоті
        (виглядає так само, як "нерухомо").
      - NHC/airspeed писані під координований політ літака з фіксованим
        крилом і фізично не мають сенсу для мультиротора.
      На реальних тестових даних (README, "Стан і результати") ZUPT і NHC
      обидва виявились шкідливими саме на польоті ЛІТАКА — тому дефолт
      fixed_wing теж "усе вимкнено", а не "NHC увімкнений за підручником".
      Для логів без mav_type (старі логи, dataflash_to_csv.py) тип
      визначається як "unknown" — найобережніший дефолт, усе вимкнено.

    use_airspeed=True: використати колонку airspeed_ms (з піто-трубки —
    лише для літаків з фіксованим крилом; dataflash_to_csv.py заповнює її
    з ARSP). Вітер оцінюється заново при кожному reset_interval_s з GPS —
    БЕЗ оцінки вітру airspeed системно шкодить (вітер ігнорується як 0,
    що вносить постійне зміщення), тому ця функція завжди оцінює вітер,
    якщо use_airspeed=True. Найбільше допомагає на довших інтервалах між
    фіксами (де дрейф встигає накопичитись) — на дуже коротких (~5с) може
    трохи гіршити порівняно з чистим position+velocity reset.

    video_path: опційний .h264 з additional-lowercam.service (нижня/CSI-камера) для
    корекції по оптичному потоку (optical_flow/, README.md там). На
    відміну від NHC/airspeed (лише fixed-wing), потік корисний для
    БУДЬ-ЯКОГО апарата — вимірює швидкість відносно землі напряму, без
    залежності від вітру. use_flow=None (авто) — увімкнено, щойно заданий
    video_path; явний False вимикає навіть при наявному відео.
    """
    rows = load_log(csv_path)
    gps_positions = _build_gps_reference(rows)
    # GPS/local_position — ЛИШЕ еталон для вимірювання дрейфу між
    # періодичними скидами (reset_interval_s), НЕ вхід самого розрахунку
    # інерції: EKF (IMU + баро + опційно оптичний потік) рахує позицію
    # незалежно від GPS, це й є весь сенс інерціальної навігації. Без
    # еталону просто не з'являються interval_pct/interval_max_err —
    # сира траєкторія EKF рахується і повертається в обох випадках.
    has_ground_truth = gps_positions is not None

    detected_type = airframe.classify(airframe.majority_mav_type(rows))
    auto_flags = airframe.default_flags(detected_type)
    if use_zupt is None:
        use_zupt = auto_flags["use_zupt"]
    if use_nhc is None:
        use_nhc = auto_flags["use_nhc"]
    if use_airspeed is None:
        use_airspeed = auto_flags["use_airspeed"]
    if use_flow is None:
        use_flow = video_path is not None
    flow_source = RecordingFrameSource(video_path) if (use_flow and video_path) else None
    flow_estimator = OpticalFlowEstimator() if flow_source is not None else None
    flow_applied_count = 0

    if verbose:
        print(
            f"[ekf_replay] Тип апарата: {detected_type} "
            f"-> zupt={use_zupt} nhc={use_nhc} airspeed={use_airspeed} flow={use_flow}"
        )

    t_all = np.array([float(r["timestamp"]) for r in rows])
    n = len(rows)

    ekf = EKFEstimator(config or EKFConfig())
    baro_offset = float(rows[start_idx].get("baro_alt") or 0.0)
    initial_velocity = _gps_velocity_at(gps_positions, t_all, start_idx) if has_ground_truth else np.zeros(3)
    ekf.reset(position=np.zeros(3), velocity=initial_velocity)

    prev_t = None
    last_reset_t = t_all[start_idx]
    cur_max_err = 0.0
    wind_enu = np.zeros(3)
    if has_ground_truth:
        cur_start_gps = gps_positions[start_idx].copy()
        reset_ref = gps_positions[start_idx].copy()
        if use_airspeed:
            row0 = rows[start_idx]
            wind_enu = _estimate_wind(
                gps_positions, t_all, start_idx,
                deg_to_rad(float(row0["roll"])), deg_to_rad(float(row0["pitch"])), deg_to_rad(float(row0["yaw"])),
                float(row0.get("airspeed_ms") or 0.0),
            )

    timestamps, positions, errors = [], [], []
    interval_pct, interval_max_err, interval_dist = [], [], []

    for i in range(start_idx, n):
        row = rows[i]
        roll = deg_to_rad(float(row["roll"])); pitch = deg_to_rad(float(row["pitch"])); yaw = deg_to_rad(float(row["yaw"]))
        acc_body = mg_to_ms2([float(row["acc_x"]), float(row["acc_y"]), float(row["acc_z"])])
        gyro_body = np.array([float(row["gyro_x"]), float(row["gyro_y"]), float(row["gyro_z"])]) / 1000.0
        baro_alt = float(row.get("baro_alt") or 0.0)
        airspeed_ms = float(row.get("airspeed_ms") or 0.0)
        t = t_all[i]
        dt = 0.0 if prev_t is None else min(max(t - prev_t, 0.0), max_dt)
        prev_t = t

        ekf.predict(roll, pitch, yaw, acc_body, dt)
        if use_baro:
            ekf.update_baro(baro_alt, baro_offset)
        if use_zupt:
            ekf.maybe_update_zupt(acc_body, gyro_body)
        if use_nhc:
            ekf.update_nhc(roll, pitch, yaw)
        if use_airspeed and airspeed_ms > 0.5:
            ekf.update_airspeed(airspeed_ms, roll, pitch, yaw, wind_enu=wind_enu)
        if flow_source is not None:
            result = flow_source.frame_pair_at(t)
            if result is not None:
                prev_gray, gray, flow_dt = result
                flow_result = flow_estimator.estimate(
                    prev_gray, gray, altitude_m=baro_alt, gyro_body_rads=gyro_body, dt=flow_dt
                )
                if update_ekf_with_flow(ekf, flow_result, roll, pitch, yaw):
                    flow_applied_count += 1

        if not has_ground_truth:
            # Немає еталону для звірки/скидів — просто веде сиру
            # траєкторію EKF (IMU + баро + опційно потік), без interval_*.
            timestamps.append(t); positions.append(ekf.position.copy())
            continue

        gps_now = gps_positions[i]
        abs_pos = ekf.position + reset_ref
        err = np.linalg.norm(abs_pos - gps_now)
        cur_max_err = max(cur_max_err, err)

        timestamps.append(t); positions.append(abs_pos.copy()); errors.append(err)

        if t - last_reset_t >= reset_interval_s:
            dist = np.linalg.norm(gps_now - cur_start_gps)
            interval_pct.append(100 * cur_max_err / dist if dist > 1e-6 else 0.0)
            interval_max_err.append(cur_max_err)
            interval_dist.append(dist)
            ekf.update_position(np.zeros(3), std=pos_std)
            ekf.update_velocity(_gps_velocity_at(gps_positions, t_all, i), std=vel_std)
            if use_airspeed:
                wind_enu = _estimate_wind(gps_positions, t_all, i, roll, pitch, yaw, airspeed_ms)
            baro_offset = baro_alt
            reset_ref = gps_now.copy(); cur_start_gps = gps_now.copy(); last_reset_t = t; cur_max_err = 0.0

    if flow_source is not None:
        flow_source.close()

    if not has_ground_truth:
        positions_arr = np.array(positions)
        displacement = (
            float(np.linalg.norm(positions_arr[-1] - positions_arr[0])) if len(positions_arr) > 1 else 0.0
        )
        path_length = (
            float(np.sum(np.linalg.norm(np.diff(positions_arr, axis=0), axis=1))) if len(positions_arr) > 1 else 0.0
        )
        return {
            "has_ground_truth": False,
            "timestamps": np.array(timestamps),
            "positions": positions_arr,
            "final_position": positions_arr[-1] if len(positions_arr) else np.zeros(3),
            "displacement_m": displacement,
            "path_length_m": path_length,
            "flow_applied_count": flow_applied_count,
        }

    return {
        "has_ground_truth": True,
        "timestamps": np.array(timestamps),
        "positions": np.array(positions),
        "gps_positions": gps_positions[start_idx:],
        "errors": np.array(errors),
        "interval_pct": np.array(interval_pct),
        "interval_max_err": np.array(interval_max_err),
        "interval_dist": np.array(interval_dist),
        "flow_applied_count": flow_applied_count,
    }


def _tristate(value: str):
    return {"auto": None, "on": True, "off": False}[value]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--pos-std", type=float, default=0.5)
    parser.add_argument("--vel-std", type=float, default=0.2)
    parser.add_argument("--zupt", choices=["auto", "on", "off"], default="auto",
                         help="auto (за замовчуванням) = визначити з mav_type у лозі (див. airframe.py)")
    parser.add_argument("--nhc", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--airspeed", choices=["auto", "on", "off"], default="auto",
                         help="Використати airspeed_ms (піто-трубка, лише літаки) з оцінкою вітру на кожному фіксі")
    parser.add_argument("--video", default=None,
                         help="Опційний .h264 з additional-lowercam.service (нижня/CSI-камера) для корекції по оптичному потоку. "
                              "Ім'я файлу має бути rec_YYYYMMDD_HHMMSS.h264 (як пише lowercam_capture.py) — час старту "
                              "розпізнається з нього для синхронізації з CSV.")
    parser.add_argument("--flow", choices=["auto", "on", "off"], default="auto",
                         help="auto (за замовчуванням) = увімкнено, якщо задано --video")
    args = parser.parse_args()

    result = run(
        args.csv_path, reset_interval_s=args.interval,
        pos_std=args.pos_std, vel_std=args.vel_std,
        use_zupt=_tristate(args.zupt), use_nhc=_tristate(args.nhc), use_airspeed=_tristate(args.airspeed),
        video_path=args.video, use_flow=_tristate(args.flow),
    )
    if not result["has_ground_truth"]:
        print(f"{args.csv_path}: немає GPS/local_position — звірку дрейфу пропущено, лише сира траєкторія EKF.")
        print(f"  зміщення старт->кінець: {result['displacement_m']:.1f}м, пройдений шлях: {result['path_length_m']:.1f}м")
        if args.video:
            print(f"  optical flow: застосовано {result['flow_applied_count']} корекцій")
    else:
        pct = result["interval_pct"]
        err = result["interval_max_err"]
        print(f"{args.csv_path}: інтервал корекції {args.interval:.0f}с, n={len(pct)} інтервалів")
        if len(pct):
            print(f"  медіана % від пройденого шляху: {np.median(pct):.1f}%")
            print(f"  95-й перцентиль: {np.percentile(pct, 95):.1f}%")
            print(f"  максимум: {pct.max():.1f}%")
            print(f"  медіана похибки: {np.median(err):.1f}м, макс похибки: {err.max():.1f}м")
        if args.video:
            print(f"  optical flow: застосовано {result['flow_applied_count']} корекцій")
