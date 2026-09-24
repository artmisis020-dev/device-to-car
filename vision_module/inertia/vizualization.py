"""Анімована 3D-візуалізація траєкторії дрона, розрахованої InertialEstimator.

Раніше цей файл сам рахував траєкторію (calculate_trajectory) з дубльованою
rotation_matrix і евристичним інтегруванням — тепер розрахунок винесено
в estimator.py/replay.py, а тут лишається тільки відображення. Залежність
від pandas прибрано — вистачає numpy.
"""
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.lines import Line2D

from replay import run as run_replay


class DroneVisualizer:
    def __init__(self, data_file, start_s=0.0, duration_s=None):
        """start_s/duration_s — вікно за часом від початку логу (секунди).

        На довгих логах (десятки хвилин) чиста інерційка неминуче
        розходиться до нефізичних значень (див. примітку в replay.py) —
        накладена на той самий графік GPS-траєкторія стає невидимою
        крапкою. duration_s дозволяє звузити анімацію до вікна, де
        порівняння інерційка-vs-GPS ще має сенс (напр. перші 60-120с).
        """
        result = run_replay(data_file)
        positions = result["positions"]
        attitudes_deg = result["attitudes_deg"]
        gps_positions = result["gps_positions"]
        timestamps = result["timestamps"]

        if len(timestamps) > 1:
            t0 = timestamps[0]
            lo = t0 + start_s
            hi = (t0 + start_s + duration_s) if duration_s is not None else timestamps[-1]
            mask = (timestamps >= lo) & (timestamps <= hi)
            if mask.any():
                positions = positions[mask]
                attitudes_deg = attitudes_deg[mask]
                if gps_positions is not None:
                    gps_positions = gps_positions[mask]

        self.positions = positions
        self.attitudes_deg = attitudes_deg
        self.used_baro = result["used_baro"]
        self.used_gps = result["used_gps"]
        self.gps_positions = gps_positions  # None, якщо в лозі немає реального GPS
        if self.used_gps:
            self.gps_error = np.linalg.norm(self.positions - self.gps_positions, axis=1)

        self.fig = plt.figure(figsize=(18, 10))
        grid = plt.GridSpec(2, 3, height_ratios=[1, 2])

        self.ax_roll = self.fig.add_subplot(grid[0, 0])
        self.ax_pitch = self.fig.add_subplot(grid[0, 1])
        self.ax_yaw = self.fig.add_subplot(grid[0, 2])
        self.ax_altitude = self.fig.add_subplot(grid[1, 0])
        self.ax_traj_2d = self.fig.add_subplot(grid[1, 1])
        self.ax_traj = self.fig.add_subplot(grid[1, 2], projection='3d')

        self.setup_plots()
        self.setup_lines()
        self.current_frame = 0

    def create_drone_model(self, size=0.3):
        arm_length = size
        arms = np.array([
            [arm_length, 0, 0],
            [0, arm_length, 0],
            [-arm_length, 0, 0],
            [0, -arm_length, 0],
        ])
        prop_height = size * 0.1
        props = np.array([
            [arm_length, 0, prop_height],
            [0, arm_length, prop_height],
            [-arm_length, 0, prop_height],
            [0, -arm_length, prop_height],
        ])
        return np.array([0, 0, 0]), arms, props

    def setup_plots(self):
        self.ax_roll.set_title('Roll (Крен)', fontsize=12)
        self.ax_roll.set_xlim(-1.5, 1.5)
        self.ax_roll.set_ylim(-1.5, 1.5)
        self.ax_roll.grid(True)
        self.ax_roll.set_aspect('equal')
        self.ax_roll.set_xlabel('X')
        self.ax_roll.set_ylabel('Y')

        self.ax_pitch.set_title('Pitch (Тангаж)', fontsize=12)
        self.ax_pitch.set_xlim(-1.5, 1.5)
        self.ax_pitch.set_ylim(-1.5, 1.5)
        self.ax_pitch.grid(True)
        self.ax_pitch.set_aspect('equal')
        self.ax_pitch.set_xlabel('X')
        self.ax_pitch.set_ylabel('Y')

        self.ax_yaw.set_title('Yaw (Рискання)', fontsize=12)
        self.ax_yaw.set_xlim(-1.5, 1.5)
        self.ax_yaw.set_ylim(-1.5, 1.5)
        self.ax_yaw.grid(True)
        self.ax_yaw.set_aspect('equal')
        self.ax_yaw.set_xlabel('X')
        self.ax_yaw.set_ylabel('Y')

        all_pos = self.positions if not self.used_gps else np.vstack([self.positions, self.gps_positions])

        baro_note = ' (з баро)' if self.used_baro else ' (тільки IMU, баро недоступне в лозі)'
        self.ax_altitude.set_title(f'Висота дрона з часом{baro_note}', fontsize=12)
        max_alt = max(np.max(np.abs(all_pos[:, 2])) * 1.2, 1e-3)
        self.ax_altitude.set_xlim(0, len(self.positions))
        self.ax_altitude.set_ylim(-max_alt, max_alt)
        self.ax_altitude.grid(True)
        self.ax_altitude.set_xlabel('Кадр')
        self.ax_altitude.set_ylabel('Висота (м)')

        gps_note = ' (з GPS-звіркою)' if self.used_gps else ' (GPS у лозі немає)'
        self.ax_traj_2d.set_title(f'2D Траєкторія (вид зверху){gps_note}', fontsize=12)
        max_range = max(np.max(np.abs(all_pos[:, :2])) * 1.2, 1e-3)
        self.ax_traj_2d.set_xlim(-max_range, max_range)
        self.ax_traj_2d.set_ylim(-max_range, max_range)
        self.ax_traj_2d.grid(True)
        self.ax_traj_2d.set_aspect('equal')
        self.ax_traj_2d.set_xlabel('X (м)')
        self.ax_traj_2d.set_ylabel('Y (м)')
        self.ax_traj_2d.axhline(y=0, color='k', linestyle='--', alpha=0.3)
        self.ax_traj_2d.axvline(x=0, color='k', linestyle='--', alpha=0.3)

        self.ax_traj.set_title('3D Траєкторія польоту', fontsize=14)
        max_range_3d = max(np.max(np.abs(all_pos)) * 1.2, 1e-3)
        self.ax_traj.set_xlim(-max_range_3d, max_range_3d)
        self.ax_traj.set_ylim(-max_range_3d, max_range_3d)
        self.ax_traj.set_zlim(-max_range_3d, max_range_3d)
        self.ax_traj.set_xlabel('X (м)', fontsize=12)
        self.ax_traj.set_ylabel('Y (м)', fontsize=12)
        self.ax_traj.set_zlabel('Z (м) - Висота', fontsize=12)

        x_grid, y_grid = np.meshgrid(
            np.linspace(-max_range_3d, max_range_3d, 10),
            np.linspace(-max_range_3d, max_range_3d, 10),
        )
        z_grid = np.zeros_like(x_grid)
        self.ax_traj.plot_surface(x_grid, y_grid, z_grid, alpha=0.1, color='gray')

        self.ax_traj.quiver(0, 0, 0, max_range_3d * 0.3, 0, 0, color='red', arrow_length_ratio=0.1, label='X')
        self.ax_traj.quiver(0, 0, 0, 0, max_range_3d * 0.3, 0, color='green', arrow_length_ratio=0.1, label='Y')
        self.ax_traj.quiver(0, 0, 0, 0, 0, max_range_3d * 0.3, color='blue', arrow_length_ratio=0.1, label='Z')
        self.ax_traj.legend()

        legend_elements = [
            Line2D([0], [0], marker='o', color='w', markerfacecolor='r', markersize=10, label='Передній'),
            Line2D([0], [0], marker='o', color='w', markerfacecolor='g', markersize=10, label='Правий'),
            Line2D([0], [0], marker='o', color='w', markerfacecolor='b', markersize=10, label='Задній'),
            Line2D([0], [0], marker='o', color='w', markerfacecolor='y', markersize=10, label='Лівий'),
        ]
        if self.used_gps:
            legend_elements.append(Line2D([0], [0], color='m', linestyle='--', lw=2, label='GPS (еталон)'))
        self.ax_traj.legend(handles=legend_elements, loc='upper right')

    def setup_lines(self):
        self.line_roll, = self.ax_roll.plot([], [], 'b-', lw=2)
        self.line_pitch, = self.ax_pitch.plot([], [], 'g-', lw=2)
        self.line_yaw, = self.ax_yaw.plot([], [], 'r-', lw=2)

        self.line_altitude, = self.ax_altitude.plot([], [], 'b-', lw=2)
        self.point_altitude, = self.ax_altitude.plot([], [], 'ro', markersize=6)

        self.line_traj_2d, = self.ax_traj_2d.plot([], [], 'b-', lw=1.5)
        self.point_traj_2d, = self.ax_traj_2d.plot([], [], 'ro', markersize=8)
        self.drone_direction_2d = self.ax_traj_2d.quiver(0, 0, 1, 0, color='r', scale=5)

        self.line_traj, = self.ax_traj.plot3D([], [], [], 'b-', lw=1)

        if self.used_gps:
            self.line_traj_2d_gps, = self.ax_traj_2d.plot([], [], 'm--', lw=1.5)
            self.line_traj_gps, = self.ax_traj.plot3D([], [], [], 'm--', lw=1)

        self.drone_arms = [
            self.ax_traj.plot3D([], [], [], 'r-', lw=2)[0],
            self.ax_traj.plot3D([], [], [], 'g-', lw=2)[0],
            self.ax_traj.plot3D([], [], [], 'b-', lw=2)[0],
            self.ax_traj.plot3D([], [], [], 'y-', lw=2)[0],
        ]
        self.drone_props = [
            self.ax_traj.plot3D([], [], [], 'ro', markersize=6)[0],
            self.ax_traj.plot3D([], [], [], 'go', markersize=6)[0],
            self.ax_traj.plot3D([], [], [], 'bo', markersize=6)[0],
            self.ax_traj.plot3D([], [], [], 'yo', markersize=6)[0],
        ]
        self.drone_center = self.ax_traj.plot3D([], [], [], 'ko', markersize=8)[0]
        self.drone_altitude_line = self.ax_traj.plot3D([], [], [], 'k--', lw=1)[0]

    def update_drone_model(self, position, roll, pitch, yaw):
        from imu_math import rotation_matrix

        center, arms, props = self.create_drone_model()
        R = rotation_matrix(roll, pitch, yaw)

        arms_positioned = np.array([R @ arm for arm in arms]) + position
        props_positioned = np.array([R @ prop for prop in props]) + position

        self.drone_center.set_data_3d([position[0]], [position[1]], [position[2]])
        for i, arm in enumerate(self.drone_arms):
            arm.set_data_3d(
                [position[0], arms_positioned[i, 0]],
                [position[1], arms_positioned[i, 1]],
                [position[2], arms_positioned[i, 2]],
            )
        for i, prop in enumerate(self.drone_props):
            prop.set_data_3d([props_positioned[i, 0]], [props_positioned[i, 1]], [props_positioned[i, 2]])

        self.drone_altitude_line.set_data_3d(
            [position[0], position[0]], [position[1], position[1]], [position[2], 0],
        )

    def update(self, frame):
        self.current_frame = frame

        roll, pitch, yaw = self.attitudes_deg[frame]
        roll_rad, pitch_rad, yaw_rad = np.radians([roll, pitch, yaw])

        x = np.array([-1, 1])
        y = np.zeros_like(x)

        roll_x = x * np.cos(roll_rad) - y * np.sin(roll_rad)
        roll_y = x * np.sin(roll_rad) + y * np.cos(roll_rad)
        self.line_roll.set_data(roll_x, roll_y)

        pitch_x = x * np.cos(pitch_rad) - y * np.sin(pitch_rad)
        pitch_y = x * np.sin(pitch_rad) + y * np.cos(pitch_rad)
        self.line_pitch.set_data(pitch_x, pitch_y)

        yaw_x = x * np.cos(yaw_rad) - y * np.sin(yaw_rad)
        yaw_y = x * np.sin(yaw_rad) + y * np.cos(yaw_rad)
        self.line_yaw.set_data(yaw_x, yaw_y)

        frames = np.arange(frame + 1)
        altitudes = self.positions[:frame + 1, 2]
        self.line_altitude.set_data(frames, altitudes)
        self.point_altitude.set_data([frame], [altitudes[-1]])

        positions = self.positions[:frame + 1]
        current_pos = positions[-1]

        self.line_traj_2d.set_data(positions[:, 0], positions[:, 1])
        self.point_traj_2d.set_data([current_pos[0]], [current_pos[1]])

        direction_x = np.cos(yaw_rad)
        direction_y = np.sin(yaw_rad)
        self.drone_direction_2d.remove()
        self.drone_direction_2d = self.ax_traj_2d.quiver(
            current_pos[0], current_pos[1], direction_x, direction_y, color='r', scale=5, width=0.005,
        )

        self.line_traj.set_data_3d(positions[:, 0], positions[:, 1], positions[:, 2])
        self.update_drone_model(current_pos, roll_rad, pitch_rad, yaw_rad)

        if self.used_gps:
            gps_positions = self.gps_positions[:frame + 1]
            self.line_traj_2d_gps.set_data(gps_positions[:, 0], gps_positions[:, 1])
            self.line_traj_gps.set_data_3d(gps_positions[:, 0], gps_positions[:, 1], gps_positions[:, 2])

        self.ax_roll.set_title(f'Roll (Крен): {roll:.1f}°', fontsize=12)
        self.ax_pitch.set_title(f'Pitch (Тангаж): {pitch:.1f}°', fontsize=12)
        self.ax_yaw.set_title(f'Yaw (Рискання): {yaw:.1f}°', fontsize=12)
        self.ax_altitude.set_title(f'Висота: {current_pos[2]:.2f} м', fontsize=12)

        info_text = (
            f"Кадр: {frame}/{len(self.positions) - 1}\n"
            f"X: {current_pos[0]:.2f} м\nY: {current_pos[1]:.2f} м\nZ: {current_pos[2]:.2f} м"
        )
        if self.used_gps:
            info_text += f"\nПохибка vs GPS: {self.gps_error[frame]:.2f} м"
        if hasattr(self, 'info_text_artist'):
            self.info_text_artist.remove()
        self.info_text_artist = self.ax_traj.text2D(
            0.05, 0.95, info_text, transform=self.ax_traj.transAxes, fontsize=12,
            verticalalignment='top', bbox=dict(boxstyle='round', facecolor='white', alpha=0.6),
        )

        elements = [
            self.line_roll, self.line_pitch, self.line_yaw,
            self.line_altitude, self.point_altitude,
            self.line_traj_2d, self.point_traj_2d,
            self.line_traj, self.drone_center,
        ]
        elements.extend(self.drone_arms)
        elements.extend(self.drone_props)
        elements.append(self.drone_altitude_line)
        if self.used_gps:
            elements.extend([self.line_traj_2d_gps, self.line_traj_gps])
        return tuple(elements)

    def animate(self):
        ani = FuncAnimation(
            self.fig, self.update, frames=len(self.positions), interval=100, blit=False,
        )
        plt.tight_layout()
        plt.show()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Анімована візуалізація траєкторії дрона (inertial vs GPS)")
    parser.add_argument("data_file", nargs="?", default="flight_logs.csv")
    parser.add_argument("--start", type=float, default=0.0, help="Початок вікна, с від початку логу")
    parser.add_argument("--duration", type=float, default=None, help="Тривалість вікна, с (за замовчуванням — весь лог)")
    args = parser.parse_args()

    visualizer = DroneVisualizer(args.data_file, start_s=args.start, duration_s=args.duration)
    visualizer.animate()
