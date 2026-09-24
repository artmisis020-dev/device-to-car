"""Статичне порівняння всього маршруту: як летів дрон за GPS vs як порахувала
інерційка (estimator.py), одним поглядом на всю довжину логу — без покадрової
анімації (для цього є vizualization.py).

Показує 4 панелі:
  1. GPS-маршрут (вид зверху) в його власному масштабі — реальна форма польоту.
  2. Той самий GPS + inertial накладені один на одного в спільному масштабі —
     видно, наскільки і в який бік інерційка "з'їжджає" з реального маршруту.
  3. Висота (Z) GPS vs inertial у часі — тут масштаб завжди порівнюваний
     (баро не дрейфує так, як горизонтальне подвійне інтегрування).
  4. Похибка |inertial − GPS| у часі (лог-шкала) — де і як швидко росте
     розбіжність; якщо доступний gps_integrity.py, підсвічує spoof/jam-епізоди
     для контексту (чи похибка росте "природно", чи саме через spoof/jam).

Використання:
    python3 route_summary.py лог.csv [--out route_summary.png]
"""
from __future__ import annotations

import argparse

import numpy as np
import matplotlib.pyplot as plt

from replay import run as run_replay


def _path_length(positions):
    return float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1)))


def summarize(csv_path):
    result = run_replay(csv_path)
    pos = result["positions"]
    t = result["timestamps"]
    summary = {
        "duration_s": float(t[-1] - t[0]) if len(t) > 1 else 0.0,
        "n_samples": len(pos),
        "inertial_path_length_m": _path_length(pos),
        "inertial_final_pos": pos[-1],
        "used_gps": result["used_gps"],
    }
    if result["used_gps"]:
        gps = result["gps_positions"]
        error = np.linalg.norm(pos - gps, axis=1)
        summary.update({
            "gps_path_length_m": _path_length(gps),
            "gps_final_pos": gps[-1],
            "error_final_m": float(error[-1]),
            "error_mean_m": float(error.mean()),
            "error_max_m": float(error.max()),
            "error_max_t": float(t[np.argmax(error)] - t[0]),
        })
    return result, summary


def plot_route_summary(csv_path, out_path=None, show_integrity=True):
    result, summary = summarize(csv_path)
    t0 = result["timestamps"][0]
    t = result["timestamps"] - t0
    pos = result["positions"]
    has_gps = result["used_gps"]

    fig = plt.figure(figsize=(16, 11))
    grid = plt.GridSpec(2, 2, figure=fig, height_ratios=[2, 1])
    ax_overlay = fig.add_subplot(grid[0, :])
    ax_alt = fig.add_subplot(grid[1, 0])
    ax_error = fig.add_subplot(grid[1, 1])

    if has_gps:
        gps = result["gps_positions"]
        error = np.linalg.norm(pos - gps, axis=1)

        # GPS і inertial на ОДНИХ осях — щоб різницю було видно одразу, наочно,
        # без перемикання між панелями. Коли inertial-дрейф на порядки більший
        # за GPS-маршрут (довгі логи без корекції), GPS все одно лишається
        # видимим завдяки товщій лінії/маркерам і підписаному масштабному
        # застереженню — а сама похибка (праворуч) дає точні числа.
        ax_overlay.plot(gps[:, 0], gps[:, 1], color='tab:green', lw=2.2, label='GPS (еталон)', zorder=3)
        ax_overlay.plot(pos[:, 0], pos[:, 1], color='tab:blue', lw=1.2, alpha=0.85, label='Inertial (estimator.py)', zorder=2)
        ax_overlay.plot(gps[0, 0], gps[0, 1], 'o', color='black', markersize=8, label='старт', zorder=4)
        ax_overlay.plot(gps[-1, 0], gps[-1, 1], 's', color='tab:green', markersize=9, zorder=4)
        ax_overlay.plot(pos[-1, 0], pos[-1, 1], 's', color='tab:blue', markersize=9, zorder=4)

        gps_extent = float(np.max(np.abs(gps[:, :2]))) if len(gps) else 0.0
        inertial_extent = float(np.max(np.abs(pos[:, :2]))) if len(pos) else 0.0
        scale_ratio = max(inertial_extent, gps_extent) / max(min(inertial_extent, gps_extent), 1e-6)
        title = 'GPS vs Inertial — спільний маршрут (наочне порівняння)'
        if scale_ratio > 8:
            wider = 'inertial' if inertial_extent > gps_extent else 'GPS'
            title += (
                f'\n(масштаби відрізняються у {scale_ratio:.0f}x — {wider} домінує на осях; '
                f'похибка в часі внизу праворуч показує це точними числами)'
            )
        ax_overlay.set_title(title, fontsize=10)
        ax_overlay.legend(fontsize=9, loc='best')

        ax_alt.plot(t, gps[:, 2], color='tab:green', lw=1.2, label='GPS висота')
        ax_alt.plot(t, pos[:, 2], color='tab:blue', lw=1.0, alpha=0.8, label='Inertial висота')
        ax_alt.set_title('Висота: GPS vs Inertial')
        ax_alt.set_xlabel('Час, с')
        ax_alt.set_ylabel('м')
        ax_alt.legend(fontsize=8)

        ax_error.plot(t, np.maximum(error, 1e-3), color='tab:red', lw=1.0)
        ax_error.set_yscale('log')
        ax_error.set_title('Похибка |Inertial − GPS| (лог-шкала)')
        ax_error.set_xlabel('Час, с')
        ax_error.set_ylabel('м (log)')

        if show_integrity:
            try:
                from gps_integrity import analyze as analyze_integrity
                analysis = analyze_integrity(csv_path)
                for ep in analysis["spoof_candidates"]:
                    ax_error.axvspan(ep["start_t"] - t0, ep["end_t"] - t0, color='red', alpha=0.12, label='_spoof')
                for ep in analysis["gps_quality_dropouts"]:
                    ax_error.axvspan(ep["start_t"] - t0, ep["end_t"] - t0, color='orange', alpha=0.12, label='_jam_gnss')
                for ep in analysis["link_gaps"]:
                    ax_error.axvspan(ep["start_t"] - t0, ep["end_t"] - t0, color='purple', alpha=0.12, label='_jam_link')
                from matplotlib.patches import Patch
                handles = [
                    Patch(color='red', alpha=0.3, label='spoof (з gps_integrity.py)'),
                    Patch(color='orange', alpha=0.3, label='jam GNSS'),
                    Patch(color='purple', alpha=0.3, label='jam лінії зв\'язку'),
                ]
                ax_error.legend(handles=handles, fontsize=7, loc='upper left')
            except Exception:
                pass  # gps_integrity опційний, головний графік не має через це падати

        info = (
            f"Довжина маршруту (GPS): {summary['gps_path_length_m']:.0f} м\n"
            f"Довжина маршруту (inertial): {summary['inertial_path_length_m']:.0f} м\n"
            f"Похибка: кінцева={summary['error_final_m']:.1f} м, "
            f"середня={summary['error_mean_m']:.1f} м, "
            f"макс={summary['error_max_m']:.1f} м (на t={summary['error_max_t']:.0f}с)"
        )
    else:
        ax_alt.text(0.5, 0.5, 'GPS у цьому лозі немає', ha='center', va='center', transform=ax_alt.transAxes)
        ax_error.text(0.5, 0.5, 'GPS у цьому лозі немає — похибку показати нема з чим', ha='center', va='center', transform=ax_error.transAxes)
        ax_overlay.plot(pos[:, 0], pos[:, 1], color='tab:blue', lw=1.2, label='Inertial (estimator.py)')
        ax_overlay.plot(pos[0, 0], pos[0, 1], 'bo', markersize=8, label='старт')
        ax_overlay.plot(pos[-1, 0], pos[-1, 1], 'bs', markersize=8, label='фініш')
        ax_overlay.set_title('Inertial-маршрут (GPS для звірки немає)')
        ax_overlay.legend(fontsize=8)
        info = f"Довжина маршруту (inertial): {summary['inertial_path_length_m']:.0f} м"

    ax_overlay.set_xlabel('X, м')
    ax_overlay.set_ylabel('Y, м')
    ax_overlay.set_aspect('equal')
    ax_overlay.grid(True, alpha=0.3)

    fig.suptitle(f"{csv_path} — {summary['duration_s']:.0f}с, {summary['n_samples']} семплів\n{info}", fontsize=11)
    plt.tight_layout(rect=(0, 0, 1, 0.92))

    if out_path:
        fig.savefig(out_path, dpi=120)
        print(f"Збережено {out_path}")
    else:
        plt.show()
    return fig, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Статичне порівняння GPS vs Inertial по всьому маршруту")
    parser.add_argument("csv_path", nargs="?", default="flight_logs.csv")
    parser.add_argument("--out", default=None, help="Куди зберегти PNG (без прапорця — відкриє вікно)")
    parser.add_argument("--no-integrity", action="store_true", help="Не підсвічувати spoof/jam-епізоди на графіку похибки")
    args = parser.parse_args()

    _, summary = plot_route_summary(args.csv_path, out_path=args.out, show_integrity=not args.no_integrity)

    print(f"\nТривалість: {summary['duration_s']:.0f} с, семплів: {summary['n_samples']}")
    print(f"Довжина маршруту (inertial): {summary['inertial_path_length_m']:.0f} м")
    if summary["used_gps"]:
        print(f"Довжина маршруту (GPS):      {summary['gps_path_length_m']:.0f} м")
        print(
            f"Похибка: кінцева={summary['error_final_m']:.1f} м, "
            f"середня={summary['error_mean_m']:.1f} м, "
            f"макс={summary['error_max_m']:.1f} м (на t={summary['error_max_t']:.0f}с)"
        )
    else:
        print("У лозі немає реального GPS — звірити нема з чим.")
