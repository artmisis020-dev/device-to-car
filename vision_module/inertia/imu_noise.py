"""Allan variance для акселерометра/гіроскопа з CSV-логу — реальні шуми
IMU замість вгаданих у EKFConfig.

Що рахується (IEEE Std 952, overlapping Allan deviation σ(τ)):
  N — velocity/angle random walk (білий шум): σ(τ) на нахилі -1/2 при
      τ=1с. Одиниці: м/с²/√Гц (акселерометр), рад/с/√Гц (гіро).
  B — нестабільність зміщення (bias instability): min σ(τ) / 0.664.

Потрібен СТАЦІОНАРНИЙ відрізок з рівномірною високою частотою (апарат на
землі, мотори стоять) — найкраще dataflash IMU (dataflash_to_csv.py, sync
до IMU) або окремий запис з INS_LOG_BAT_* ArduPilot. Телеметрійний
RAW_IMU на 2Гц для цього не годиться (τ_min=0.5с — білого шуму вже не
видно). Відрізок або задається --start/--end (с від початку логу), або
шукається сам: найдовше вікно, де |gyro|<thr і | |a|-g |<thr.

ВАЖЛИВО для налаштування EKF: N — це НИЖНЯ межа для
EKFConfig.accel_noise_std. У польоті до білого шуму сенсора додаються
вібрація, похибка орієнтації (1° = 0.17 м/с²) і неточність моделі руху —
реальний q на порядки більший (на польотах вересня 2026 найкраще q≈3).
Інструмент корисний, щоб побачити, чи це взагалі шум сенсора, і для
майбутнього повного INS (bias-стани з B як random walk).

Використання:
    python3 imu_noise.py лог.csv [--start 0 --end 250]
"""
from __future__ import annotations

import argparse
import csv

import numpy as np

from imu_math import GRAVITY, mg_to_ms2


def allan_deviation(x: np.ndarray, fs: float, n_taus: int = 40):
    """Overlapping ADEV для рівномірного ряду x (частота fs). Повертає
    (taus, adev)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    theta = np.cumsum(x) / fs            # інтеграл (кут/швидкість)
    m_max = n // 3
    ms = np.unique(np.logspace(0, np.log10(max(m_max, 1)), n_taus).astype(int))
    taus, adev = [], []
    for m in ms:
        if 2 * m >= n:
            break
        d = theta[2 * m:] - 2 * theta[m:-m] + theta[:-2 * m]
        tau = m / fs
        avar = np.sum(d ** 2) / (2 * tau ** 2 * (n - 2 * m))
        taus.append(tau)
        adev.append(np.sqrt(avar))
    return np.array(taus), np.array(adev)


def noise_params(taus, adev):
    """N — з лінії нахилу -1/2 (σ(τ)=N/√τ), підігнаної по короткій частині
    кривої (τ ≤ 1с або перші точки); B — min σ / 0.664."""
    short = taus <= max(1.0, taus[min(3, len(taus) - 1)])
    N = float(np.median(adev[short] * np.sqrt(taus[short])))
    B = float(adev.min() / 0.664)
    tau_B = float(taus[np.argmin(adev)])
    return N, B, tau_B


def find_stationary(t, acc, gyro, min_len_s=30.0, gyro_thr=0.03, acc_thr=0.3):
    still = (np.linalg.norm(gyro, axis=1) < gyro_thr) & (np.abs(np.linalg.norm(acc, axis=1) - GRAVITY) < acc_thr)
    best, best_len, cur_start = None, 0.0, None
    for i, s in enumerate(np.r_[still, False]):
        if s and cur_start is None:
            cur_start = i
        elif not s and cur_start is not None:
            length = t[i - 1] - t[cur_start]
            if length > best_len:
                best, best_len = (cur_start, i - 1), length
            cur_start = None
    return best if best_len >= min_len_s else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--start", type=float, default=None, help="с від початку логу")
    ap.add_argument("--end", type=float, default=None)
    args = ap.parse_args()

    with open(args.csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    g = lambda c: np.array([float(r.get(c) or 0.0) for r in rows])  # noqa: E731
    t = g("timestamp")
    acc = mg_to_ms2(np.c_[g("acc_x"), g("acc_y"), g("acc_z")])
    gyro = np.c_[g("gyro_x"), g("gyro_y"), g("gyro_z")] / 1000.0

    # лише СВІЖІ семпли (CSV-логери тримають останнє значення між оновленнями)
    fresh = np.r_[True, np.any(np.diff(acc, axis=0) != 0, axis=1)]
    t, acc, gyro = t[fresh], acc[fresh], gyro[fresh]

    if args.start is not None or args.end is not None:
        s = t[0] + (args.start or 0.0)
        e = t[0] + args.end if args.end is not None else t[-1]
        sel = (t >= s) & (t <= e)
        i0, i1 = np.where(sel)[0][[0, -1]]
    else:
        seg = find_stationary(t, acc, gyro)
        if seg is None:
            raise SystemExit("стаціонарного відрізка ≥30с не знайдено — задай --start/--end")
        i0, i1 = seg
    tt, a, w = t[i0:i1 + 1], acc[i0:i1 + 1], gyro[i0:i1 + 1]
    fs = (len(tt) - 1) / (tt[-1] - tt[0])
    print(f"відрізок {tt[0]-t[0]:.0f}..{tt[-1]-t[0]:.0f}с від початку, {len(tt)} семплів, ~{fs:.1f}Гц")
    if fs < 20:
        print("УВАГА: частота <20Гц — білий шум (N) оцінюється ненадійно, потрібен високочастотний лог")

    for name, data, unit in (("acc", a, "м/с²"), ("gyro", w, "рад/с")):
        for k, ax in enumerate("xyz"):
            taus, adev = allan_deviation(data[:, k] - data[:, k].mean(), fs)
            N, B, tau_B = noise_params(taus, adev)
            print(f"  {name}_{ax}: N={N:.4f} {unit}/√Гц   B={B:.4f} {unit} (τ={tau_B:.1f}с)   "
                  f"std={data[:, k].std():.4f}")
    print("N акселерометра — НИЖНЯ межа EKFConfig.accel_noise_std (див. докстрінг).")


if __name__ == "__main__":
    main()
