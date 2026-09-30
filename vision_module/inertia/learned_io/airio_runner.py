"""Обгортка мережі Air-IO (https://github.com/Air-IO/Air-IO, BSD-3):
завантаження ваг і інференс у тих самих вікнах, що й авторський
inference_motion.py (1000 семплів, без перекриття, + хвіст) — на
Blackbird наш виклик дає побітово ті самі виходи, що авторські
network_output_using_gtRot (різниця ~1e-15).

Код Air-IO не копіюється в наш репо — лежить окремо (AIRIO_ROOT), тут
лише імпортується."""
from __future__ import annotations

import os
import sys

import numpy as np

DEFAULT_ROOT = os.environ.get("AIRIO_ROOT", "/opt/sirena-vision/learnedio/Air-IO")
DEFAULT_CKPT = os.environ.get(
    "AIRIO_CKPT",
    "/opt/sirena-vision/learnedio/models/AirIO_Blackbird/AirIO_Blackbird/AirIO_checkpoint/best_model.ckpt")
DEFAULT_CONF = "configs/BlackBird/motion_body_rot.conf"


class AirIOModel:
    def __init__(self, root=DEFAULT_ROOT, ckpt=DEFAULT_CKPT, conf=DEFAULT_CONF, device=None):
        import torch
        from pyhocon import ConfigFactory
        if root not in sys.path:
            sys.path.insert(0, root)
        from model import net_dict  # noqa: E402  (Air-IO)
        self.torch = torch
        self.device = device or ("cuda:0" if torch.cuda.is_available() else "cpu")
        c = ConfigFactory.parse_file(os.path.join(root, conf))
        self.net = net_dict[c.train.network](c.train).to(self.device).double()
        state = torch.load(ckpt, map_location=torch.device(self.device), weights_only=True)
        self.net.load_state_dict(state["model_state_dict"])
        self.net.eval()
        self.window = 1000

    def predict(self, prep):
        """prep — з airio_adapter.prepare_inputs (або нативний Blackbird:
        t, acc, gyro [IMU-кадр], R_it [NWU<-IMU]). Повертає dict:
        t (L,), v_imu (L,3), cov_imu (L,3), idx (L,) — індекси семплів входу."""
        import pypose as pp
        torch = self.torch
        t, acc, gyro, R = prep["t"], prep["acc"], prep["gyro"], prep["R_it"]
        n = len(t) - 1
        starts = list(range(0, max(n - self.window, 0), self.window))
        spans = [(j, j + self.window) for j in starts]
        if not spans or spans[-1][1] < n:
            spans.append((spans[-1][1] if spans else 0, n))
        out_t, out_v, out_c, out_i = [], [], [], []
        idx_all = np.arange(len(t), dtype=float)
        with torch.no_grad():
            for a, b in spans:
                if b - a < 30:          # замало для згорток мережі
                    continue
                d = lambda x: torch.tensor(x[None], dtype=torch.double, device=self.device)  # noqa: E731
                rot = pp.mat2SO3(torch.tensor(R[a:b], dtype=torch.double), check=False).Log().tensor()
                res = self.net.forward({"acc": d(acc[a:b]), "gyro": d(gyro[a:b])}, rot[None].to(self.device))
                lab_t = self.net.get_label(torch.tensor(t[a:b + 1], dtype=torch.double)[None, :, None])[0, :, 0]
                lab_i = self.net.get_label(torch.tensor(idx_all[a:b + 1])[None, :, None])[0, :, 0]
                L = res["net_vel"].shape[1]
                out_t.append(lab_t[:L].cpu().numpy())
                out_i.append(lab_i[:L].cpu().numpy().astype(int))
                out_v.append(res["net_vel"][0].cpu().numpy())
                out_c.append(res["cov"][0].cpu().numpy() if res.get("cov") is not None else np.full((L, 3), np.nan))
        return {"t": np.concatenate(out_t), "v_imu": np.concatenate(out_v),
                "cov_imu": np.concatenate(out_c), "idx": np.concatenate(out_i)}
