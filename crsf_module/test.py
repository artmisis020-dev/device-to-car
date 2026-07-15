import socket
import pygame

RPI_IP = "10.0.0.15"
UDP_PORT = 5005

CRSF_MIN = 172
CRSF_MAX = 1811
CRSF_MID = 992

# Кнопка джойстика, що перемикає режим керування MAVLINK <-> CRSF.
# За стандартом стартуємо у MAVLINK; натискання перехоплює керування на CRSF.
MODE_TOGGLE_BUTTON = 7


def to_crsf(val, min_in, max_in):
    return int((val - min_in) * (CRSF_MAX - CRSF_MIN) / (max_in - min_in) + CRSF_MIN)


pygame.init()
pygame.joystick.init()
joy = pygame.joystick.Joystick(0)
joy.init()
print(f"Підключено: {joy.get_name()}")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
channels = [CRSF_MID] * 16
clock = pygame.time.Clock()

# control_mode = "mavlink"
control_mode = "crsf"
prev_toggle_pressed = False


def send_mode(mode):
    sock.sendto(f"MODE {mode.upper()}".encode("ascii"), (RPI_IP, UDP_PORT))
    print(f"[MODE] -> {mode.upper()}")


# Повідомляємо борт про стартовий режим
send_mode(control_mode)

try:
    while True:
        pygame.event.pump()

        # Перемикання режиму керування по фронту натискання кнопки
        num_buttons = joy.get_numbuttons()
        toggle_pressed = MODE_TOGGLE_BUTTON < num_buttons and joy.get_button(MODE_TOGGLE_BUTTON)
        if toggle_pressed and not prev_toggle_pressed:
            control_mode = "crsf" if control_mode == "mavlink" else "mavlink"
            send_mode(control_mode)
        prev_toggle_pressed = toggle_pressed

        # Осі керування
        channels[0] = to_crsf(joy.get_axis(0), -1.0, 1.0)   # Roll
        channels[1] = to_crsf(joy.get_axis(1), -1.0, 1.0)   # Pitch
        channels[2] = to_crsf(joy.get_axis(2), -1.0, 1.0)   # Throttle
        channels[3] = to_crsf(joy.get_axis(3), -1.0, 1.0)   # Yaw

        # Додаткові осі -> AUX-канали
        num_axes = joy.get_numaxes()
        for i in range(4, min(num_axes, 12)):
            channels[i] = to_crsf(joy.get_axis(i), -1.0, 1.0)

        # Кнопки -> канали-перемикачі (окрім кнопки перемикання режиму)
        offset = max(4, num_axes)
        for i in range(num_buttons):
            if i == MODE_TOGGLE_BUTTON:
                continue
            if offset + i < 16:
                channels[offset + i] = CRSF_MAX if joy.get_button(i) else CRSF_MIN

        print(f"[STICKS] Roll:{channels[0]}  Pitch:{channels[1]}  Thr:{channels[2]}  Yaw:{channels[3]}  MODE:{control_mode.upper()}")
        aux_str = "  ".join(f"AUX{i+1}:{v}" for i, v in enumerate(channels[4:12]))
        print(f"[AUX]    {aux_str}")

        packet = bytearray()
        for ch in channels:
            packet.extend(int(ch).to_bytes(2, 'little'))
        sock.sendto(packet, (RPI_IP, UDP_PORT))
        clock.tick(100)

except KeyboardInterrupt:
    sock.close()
    pygame.quit()
