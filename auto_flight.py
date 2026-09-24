"""
Automatic flight toward the tracked target.

GuidanceLogic - the logic: target pixel + current attitude -> attitude command.
                Pure math, no MAVLink, so it can be tuned and tested without a drone.
AutoFlight    - the mechanism: takes the target from Vision, asks GuidanceLogic for
                a command and sends it to the flight controller (SET_ATTITUDE_TARGET).

Angles are in degrees, thrust is 0..1.
"""
from dataclasses import dataclass

import numpy as np
from pymavlink import mavutil
from pymavlink.quaternion import QuaternionBase

from util import math_formula

# type_mask for SET_ATTITUDE_TARGET, same value the drone code always used
ATTITUDE_TYPE_MASK = 32


@dataclass
class AttitudeCommand:
    yaw: float
    pitch: float
    roll: float
    thrust: float


class GuidanceLogic:
    yaw_range = 40             # yaw offset when the target is at the frame edge
    roll_range = 110           # roll when the target is at the frame edge
    roll_yaw_threshold = 10    # above this roll the yaw uses the softer 0..20 mapping
    soft_yaw_range = 20
    max_pitch_change = 2       # max pitch change per frame
    max_thrust = 0.55

    def compute(self, center, frame_size, yaw, pitch):
        """
        Main algorithm (was Drone.movement).
        :param center: (x, y) of the target in the frame
        :param frame_size: (width, height) of the frame
        :param yaw: current drone yaw
        :param pitch: current drone pitch
        """
        center_x, center_y = center
        width, height = frame_size

        yaw_angle = math_formula.linear_mapping(center_x, 0, width, -self.yaw_range, self.yaw_range)
        roll_angle = self.roll_angle(center_x, width)
        thrust = self.thrust(center_y, height)
        pitch_angle = self.smooth_pitch(self.zone_pitch(center_y, height), pitch)

        if roll_angle > self.roll_yaw_threshold:
            yaw_angle = math_formula.linear_mapping(center_x, width / 2, width, 0, self.soft_yaw_range)
        elif roll_angle < -self.roll_yaw_threshold:
            yaw_angle = math_formula.linear_mapping(center_x, 0, width / 2, 0, -self.soft_yaw_range)

        return AttitudeCommand(self.global_yaw(yaw, yaw_angle), pitch_angle, roll_angle, thrust)

    def compute_v2(self, center, frame_size, yaw, pitch):
        """Alternative algorithm (was Drone.movement_1): adaptive yaw sensitivity, roll/pitch coupling."""
        center_x, center_y = center
        width, height = frame_size

        center_offset_x = abs(center_x - width / 2)
        center_offset_y = abs(center_y - height / 2)

        yaw_sensitivity = self.yaw_sensitivity(center_offset_x, width)
        yaw_angle = math_formula.linear_mapping(center_x, 0, width,
                                                -40 * yaw_sensitivity, 40 * yaw_sensitivity)
        pitch_angle = self.improved_pitch(center_y, height, center_offset_y)
        roll_angle = self.improved_roll(center_x, width, yaw_angle)
        thrust = self.adaptive_thrust(center_y, height)
        final_pitch = self.correct_pitch_for_roll(pitch_angle, roll_angle)

        return AttitudeCommand(self.global_yaw(yaw, yaw_angle), final_pitch, roll_angle, thrust)

    @staticmethod
    def global_yaw(yaw, yaw_angle):
        """Current yaw + offset, wrapped to -180..180."""
        global_yaw = int(yaw + yaw_angle) % 360
        if global_yaw > 180:
            global_yaw -= 360
        return global_yaw

    def roll_angle(self, center_x, width):
        return math_formula.linear_mapping(center_x, 0, width, -self.roll_range, self.roll_range)

    @staticmethod
    def zone_pitch(center_y, height, upper_pct=35, middle_pct=40):
        """
        Target pitch by the vertical zone of the target:
        upper zone -35, middle zone -13..-25, lower zone -5.
        middle_pct=25 is the previous tuning (Drone.calculate_global_pitch).
        """
        upper_threshold = (upper_pct / 100) * height
        middle_threshold = upper_threshold + (middle_pct / 100) * height

        if center_y <= upper_threshold:
            return -35
        if center_y <= middle_threshold:
            return math_formula.linear_mapping(center_y, upper_threshold, middle_threshold, -13, -25)
        return -5

    def smooth_pitch(self, target_pitch, current_pitch):
        """Limit the pitch change per frame to max_pitch_change."""
        difference = target_pitch - current_pitch
        if abs(difference) > self.max_pitch_change:
            return current_pitch + np.sign(difference) * self.max_pitch_change
        return target_pitch

    def thrust(self, center_y, height, upper_pct=35, middle_pct=45):
        """Thrust 0.5..0.15 across the upper and middle zones, 0 below them."""
        upper_threshold = (upper_pct / 100) * height
        middle_threshold = upper_threshold + (middle_pct / 100) * height

        if center_y <= middle_threshold:
            thrust = math_formula.linear_mapping(center_y, upper_threshold, middle_threshold, 0.5, 0.15)
        else:
            thrust = 0
        return min(thrust, self.max_thrust)

    @staticmethod
    def yaw_sensitivity(center_offset, width):
        """Адаптивна чутливість яв в залежності від відстані до центру"""
        max_sensitivity = 1.2
        min_sensitivity = 0.3
        normalized_offset = center_offset / (width / 2)
        return math_formula.linear_mapping(normalized_offset, 0, 1, max_sensitivity, min_sensitivity)

    @staticmethod
    def improved_pitch(center_y, height, center_offset_y):
        """Покращений розрахунок піч з плавним переходом та корекцією на відстань"""
        normalized_y = center_y / height

        if normalized_y < 0.35:  # Верхня зона
            correction = math_formula.linear_mapping(center_offset_y, 0, height / 2, 0, 10)
            return -35 + correction
        if normalized_y < 0.60:  # Середня зона
            return math_formula.linear_mapping(center_y, 0.35 * height, 0.60 * height, -30, -17)
        return -10  # Нижня зона

    @staticmethod
    def improved_roll(center_x, width, yaw_angle):
        """Покращений розрахунок ролу з урахуванням швидкості повороту"""
        base_roll = math_formula.linear_mapping(center_x, 0, width, -20, 20)
        yaw_compensation = abs(yaw_angle) * 0.2  # Зменшуємо рол при великих значеннях яв
        return base_roll * (1 - yaw_compensation)

    @staticmethod
    def adaptive_thrust(center_y, height):
        """Thrust by three zones: 0.55 upper, 0.5..0.15 middle, 0 lower."""
        upper_zone = 0.25 * height
        middle_zone = 0.75 * height

        if center_y <= upper_zone:
            return 0.55
        if center_y <= middle_zone:
            return math_formula.linear_mapping(center_y, upper_zone, middle_zone, 0.5, 0.15)
        return 0.0

    @staticmethod
    def correct_pitch_for_roll(pitch_angle, roll_angle):
        max_correction = 50
        correction = math_formula.linear_mapping(abs(roll_angle), 0, 40, 0, max_correction)
        return pitch_angle + correction


class AutoFlight:
    def __init__(self, drone, guidance=None):
        self.drone = drone
        self.guidance = guidance or GuidanceLogic()
        self.last_command = None

    def step(self, vision):
        """
        One control cycle: compute the command for the tracked target and send it
        when the pilot allowed the flight. Returns the command or None without a target.
        """
        target = vision.returnCenterOfTarget()
        if target is None:
            self.last_command = None
            return None

        height, width = vision.frame.shape[:2]
        command = self.guidance.compute(target, (width, height), self.drone.yaw, self.drone.pitch)
        self.drone.thrust = command.thrust
        self.last_command = command

        self.drone.log_obj.info(
            f"auto flight target {target}, command {command}, "
            f"real yaw {self.drone.yaw}, pitch {self.drone.pitch}, roll {self.drone.roll}")

        if self.drone.flyPermission:
            self.send(command)
            self.drone.master.recv_match(blocking=False)
        return command

    def hold(self, thrust, yaw, pitch):
        """Failsafe: level roll, keep the given yaw and pitch."""
        self.send(AttitudeCommand(yaw, pitch, 0, thrust))

    def send(self, command):
        drone = self.drone
        drone.q = QuaternionBase([np.radians(command.roll), np.radians(command.pitch), np.radians(command.yaw)])
        drone.master.mav.send(mavutil.mavlink.MAVLink_set_attitude_target_message(
            0, drone.master.target_system, drone.master.target_component, ATTITUDE_TYPE_MASK,
            drone.q, 0, 0, 0, command.thrust))
