"""Евристичне виявлення джемінгу (jam) і підміни (spoof) GPS/GNSS у лозі.

НЕ сертифікований EW-детектор — три незалежні перевірки поверх телеметрії,
які добре розрізняють два принципово різні сценарії:

  JAM (глушіння) — сигнал просто зникає/деградує:
    - find_link_gaps(): потік MAVLink-повідомлень різко сповільнюється чи
      зникає (це б'є і по інерційці — RAW_IMU/ATTITUDE так само перестають
      оновлюватись, тому "гепи в інерційці" — це і є ознака джему лінії).
    - find_gps_quality_dropouts(): GPS-приймач сам сигналізує про це —
      fix_type падає, eph/супутники погіршуються.

  SPOOF (підміна) — сигнал є і виглядає "впевненим", але бреше:
    - find_spoof_candidates(): приймач і далі рапортує хороший fix_type/eph,
      але позиція (LOCAL_POSITION_NED) фізично несумісна з власною ж
      Doppler-швидкістю приймача (gps_vel_cms) — підмінене позиційне рішення
      і непідмінений (чи по-іншому підмінений) канал швидкості розходяться.
      Класична ознака: справжній GPS не може одночасно репортувати "позиція
      щойно стрибнула на сотні м/с" і "швидкість 50 м/с" в тому самому пакеті.

Довгострокова інерційна екстраполяція для цього НЕ потрібна (і не годиться —
вона сама неминуче розходиться за хвилини, див. примітку в replay.py):
spoof-перевірка порівнює GPS сам із собою (позиція vs Doppler), а не з IMU.
"""
from __future__ import annotations

import numpy as np

from replay import load_log, run as run_replay


def _episodes_from_flags(idx, timestamps, merge_gap_s, extra=None):
    """Групує індекси, де щось "підозріло", у неперервні епізоди (мерджить
    сусідні спрацювання, що розділені не більш ніж merge_gap_s секунд)."""
    episodes = []
    for pos, i in enumerate(idx):
        extra_val = extra[pos] if extra is not None else None
        if episodes and timestamps[i] - episodes[-1]["end_t"] <= merge_gap_s:
            ep = episodes[-1]
            ep["end_t"] = timestamps[i]
            ep["n_samples"] += 1
            if extra_val is not None:
                ep["peak"] = max(ep["peak"], extra_val)
        else:
            episodes.append({
                "start_t": timestamps[i], "end_t": timestamps[i],
                "n_samples": 1, "peak": extra_val if extra_val is not None else 0.0,
            })
    return episodes


def find_link_gaps(timestamps, factor=5.0, min_gap_s=0.5, merge_gap_s=5.0):
    """Періоди, коли потік MAVLink-повідомлень (будь-яких — і GPS, і IMU)
    різко сповільнювався чи зникав. Впливає на все, включно з інерційним
    розрахунком (звідси "гепи в інерційці") — типова ознака джему лінії зв'язку.
    """
    timestamps = np.asarray(timestamps)
    dt = np.diff(timestamps)
    positive = dt[dt > 0]
    median_dt = float(np.median(positive)) if len(positive) else 0.1
    threshold = max(min_gap_s, median_dt * factor)

    gap_idx = np.where(dt > threshold)[0]
    episodes = _episodes_from_flags(gap_idx, timestamps[1:], merge_gap_s, extra=dt[gap_idx])
    for ep in episodes:
        ep["total_lost_s"] = ep.pop("peak")
    return episodes


def find_gps_quality_dropouts(csv_path, min_fix_type=3, max_eph=500, merge_gap_s=5.0):
    """Періоди після першого успішного фіксу, коли GPS-приймач сам рапортує
    погіршення (fix_type<3 і/або eph>max_eph) — ознака джему саме GNSS-сигналу
    (на відміну від spoof, де приймач і далі каже, що все добре)."""
    rows = load_log(csv_path)
    t = np.array([float(r["timestamp"]) for r in rows])
    fix_type = np.array([int(float(r.get("gps_fix_type") or 0)) for r in rows])
    eph = np.array([float(r.get("gps_eph") or 9999) for r in rows])
    sats = np.array([int(float(r.get("gps_satellites_visible") or 0)) for r in rows])

    first_fix_idx = np.argmax(fix_type >= min_fix_type) if np.any(fix_type >= min_fix_type) else None
    if first_fix_idx is None:
        return [], None

    bad = (fix_type < min_fix_type) | (eph > max_eph)
    bad[:first_fix_idx + 1] = False  # не рахуємо нормальний період до першого фіксу
    idx = np.where(bad)[0]
    episodes = _episodes_from_flags(idx, t, merge_gap_s, extra=sats[idx].astype(float))
    for ep in episodes:
        ep["min_satellites"] = int(ep.pop("peak"))
    return episodes, float(t[first_fix_idx])


def find_spoof_candidates(csv_path, residual_thresh_ms=20.0, min_fix_type=3, max_eph=300, merge_gap_s=5.0):
    """Порівнює GPS-похідну швидкість (з positions, скориговану на EKF origin
    reset у replay.run) з Doppler-швидкістю самого приймача (gps_vel_cms).
    Стабільна розбіжність при формально "хорошому" fix_type/eph — ознака,
    що позиційне рішення підмінене."""
    rows = load_log(csv_path)
    result = run_replay(csv_path)
    if not result["used_gps"]:
        return []

    t = result["timestamps"]
    gps = result["gps_positions"]
    fix_type = np.array([int(float(r.get("gps_fix_type") or 0)) for r in rows])
    eph = np.array([float(r.get("gps_eph") or 9999) for r in rows])
    vel_reported = np.array([float(r.get("gps_vel_cms") or 0) for r in rows]) / 100.0

    dt = np.diff(t)
    dist = np.linalg.norm(np.diff(gps, axis=0), axis=1)
    gps_derived_speed = np.where(dt > 0, dist / np.maximum(dt, 1e-6), 0.0)
    residual = np.abs(gps_derived_speed - vel_reported[1:])

    good_fix = (fix_type[1:] >= min_fix_type) & (eph[1:] <= max_eph)
    suspicious = good_fix & (residual > residual_thresh_ms)

    idx = np.where(suspicious)[0] + 1  # +1: компенсація зсуву від np.diff
    episodes = _episodes_from_flags(idx, t, merge_gap_s, extra=residual[idx - 1])
    for ep in episodes:
        ep["peak_residual_ms"] = ep.pop("peak")
    return episodes


def analyze(csv_path):
    timestamps = np.array([float(r["timestamp"]) for r in load_log(csv_path)])
    link_gaps = find_link_gaps(timestamps)
    gps_dropouts, first_fix_t = find_gps_quality_dropouts(csv_path)
    spoof_episodes = find_spoof_candidates(csv_path)
    return {
        "link_gaps": link_gaps,
        "gps_quality_dropouts": gps_dropouts,
        "gps_first_fix_t": first_fix_t,
        "spoof_candidates": spoof_episodes,
    }


def _fmt_span(ep, t0):
    return f"t={ep['start_t'] - t0:.1f}..{ep['end_t'] - t0:.1f}с (від початку логу), {ep['n_samples']} семплів"


def plot_report(csv_path, out_path=None):
    """Часова діаграма: розбіжність позиція/Doppler (spoof), fix_type+супутники
    (jam якості GPS) і dt між семплами (jam лінії зв'язку) з підсвіченими
    епізодами. Потребує matplotlib."""
    import matplotlib.pyplot as plt

    rows = load_log(csv_path)
    t0 = float(rows[0]["timestamp"])
    timestamps = np.array([float(r["timestamp"]) for r in rows])
    fix_type = np.array([int(float(r.get("gps_fix_type") or 0)) for r in rows])
    sats = np.array([int(float(r.get("gps_satellites_visible") or 0)) for r in rows])

    result = run_replay(csv_path)
    analysis = analyze(csv_path)

    fig, (ax_spoof, ax_quality, ax_gaps) = plt.subplots(3, 1, figsize=(14, 9), sharex=True)

    if result["used_gps"]:
        t = result["timestamps"]
        gps = result["gps_positions"]
        rows2 = rows
        vel_reported = np.array([float(r.get("gps_vel_cms") or 0) for r in rows2]) / 100.0
        dt = np.diff(t)
        dist = np.linalg.norm(np.diff(gps, axis=0), axis=1)
        gps_derived_speed = np.where(dt > 0, dist / np.maximum(dt, 1e-6), 0.0)
        residual = np.abs(gps_derived_speed - vel_reported[1:])
        ax_spoof.plot(t[1:] - t0, residual, color='tab:red', lw=0.8, label='|GPS-похідна швидкість − Doppler|')
        ax_spoof.axhline(20.0, color='gray', linestyle='--', lw=1, label='поріг spoof (20 м/с)')
        ax_spoof.set_yscale('log')
    ax_spoof.set_ylabel('м/с (log)')
    ax_spoof.set_title('SPOOF: розбіжність позиції GPS і власної Doppler-швидкості приймача')
    for ep in analysis["spoof_candidates"]:
        ax_spoof.axvspan(ep["start_t"] - t0, ep["end_t"] - t0, color='red', alpha=0.15)
    ax_spoof.legend(loc='upper right', fontsize=8)

    ax_quality.plot(timestamps - t0, sats, color='tab:blue', lw=1, label='супутники видимі')
    ax_quality2 = ax_quality.twinx()
    ax_quality2.plot(timestamps - t0, fix_type, color='tab:green', lw=1, alpha=0.6, label='fix_type')
    ax_quality2.set_ylabel('fix_type')
    ax_quality.set_ylabel('супутники')
    ax_quality.set_title('JAM (GNSS): якість фіксу — fix_type/супутники')
    gps_dropouts = analysis["gps_quality_dropouts"]
    for ep in gps_dropouts:
        ax_quality.axvspan(ep["start_t"] - t0, ep["end_t"] - t0, color='orange', alpha=0.2)

    dt_all = np.diff(timestamps)
    ax_gaps.plot(timestamps[1:] - t0, dt_all, color='tab:purple', lw=0.8, label='dt між семплами')
    ax_gaps.set_ylabel('с')
    ax_gaps.set_xlabel('Час від початку логу, с')
    ax_gaps.set_title('JAM (лінія зв\'язку): розриви потоку телеметрії (впливають і на інерційку)')
    for ep in analysis["link_gaps"]:
        ax_gaps.axvspan(ep["start_t"] - t0, ep["end_t"] - t0, color='purple', alpha=0.2)

    plt.tight_layout()
    if out_path:
        fig.savefig(out_path, dpi=110)
        print(f"Збережено графік у {out_path}")
    else:
        plt.show()
    return fig


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "flight_logs.csv"
    result = analyze(path)
    rows = load_log(path)
    t0 = float(rows[0]["timestamp"])

    print(f"=== Аналіз цілісності GPS/лінії зв'язку: {path} ===\n")

    print(f"[JAM] Розриви потоку MAVLink (впливають і на інерційку): {len(result['link_gaps'])}")
    for ep in result["link_gaps"]:
        print(f"  - {_fmt_span(ep, t0)}, втрачено {ep['total_lost_s']:.1f}с сумарно")

    print(f"\n[JAM] Деградація якості GPS-фіксу (fix_type/eph): {len(result['gps_quality_dropouts'])}")
    if result["gps_first_fix_t"] is not None:
        print(f"  (перший впевнений фікс отримано на t={result['gps_first_fix_t'] - t0:.1f}с)")
    for ep in result["gps_quality_dropouts"]:
        print(f"  - {_fmt_span(ep, t0)}, мін. супутників={ep['min_satellites']}")

    print(f"\n[SPOOF] Підозра на підміну (позиція несумісна з Doppler-швидкістю при 'хорошому' фіксі): {len(result['spoof_candidates'])}")
    for ep in result["spoof_candidates"]:
        print(f"  - {_fmt_span(ep, t0)}, пікова розбіжність={ep['peak_residual_ms']:.1f} м/с")

    if not result["link_gaps"] and not result["gps_quality_dropouts"] and not result["spoof_candidates"]:
        print("\nНічого підозрілого не знайдено (або в лозі немає GPS-даних для перевірки).")
