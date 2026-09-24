"""Експериментальний LSTM для порівняння з класичним InertialEstimator.

ВАЖЛИВО: жоден з наявних flight_logs*.csv не містить реального GPS-положення —
колонки local_x/y/z в усіх трьох логах завжди 0.0 (на польотному контролері
під час цих записів не було EKF/GPS-фіксу). Тобто незалежного ground truth
для навчання з учителем зараз немає.

За замовчуванням (target_source="estimator") скрипт тренує LSTM передбачати
траєкторію, вже розраховану класичним InertialEstimator (estimator.py) —
це НЕ незалежна перевірка точності, а навчений апроксиматор того самого
dead-reckoning. Корисно як каркас пайплайна та для порівняння "чи ML взагалі
здатен вивчити цю залежність", але не як джерело істини.

Щойно зʼявиться лог із реальним GPS-фіксом (local_x/y/z ненульові),
передайте target_source="local_position" — тоді модель вчитиметься на
справжніх мітках позиції.

Стара версія (1111.py) посилалась на неіснуючі колонки pos_x/pos_y/pos_z
і взагалі не викликала model.fit() — ваги виставлялись випадково, тобто
"прогноз" був чистим шумом. model_tets.py тренувався на повністю
синтетичних (np.random) даних і не мав жодного стосунку до реальних логів.
Обидва файли ним замінюються.
"""
from __future__ import annotations

import argparse

import numpy as np

from replay import has_real_gps, load_log, run as run_replay

FEATURE_COLUMNS = ['acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z', 'roll', 'pitch', 'yaw']
DEFAULT_SEQUENCE_LENGTH = 50


def _load_features(csv_path):
    rows = load_log(csv_path)
    return np.array([[float(r[c]) for c in FEATURE_COLUMNS] for r in rows])


def build_dataset(csv_path, target_source="estimator", sequence_length=DEFAULT_SEQUENCE_LENGTH):
    X_raw = _load_features(csv_path)

    if target_source == "local_position":
        if not has_real_gps(csv_path):
            raise ValueError(
                f"{csv_path}: local_x/y/z порожні (0.0 скрізь) — немає реального GPS "
                "ground truth у цьому лозі. Використайте target_source='estimator' "
                "або передайте лог, записаний із GPS-фіксом."
            )
        rows = load_log(csv_path)
        Y_raw = np.array([[float(r['local_x']), float(r['local_y']), float(r['local_z'])] for r in rows])
    elif target_source == "estimator":
        Y_raw = run_replay(csv_path)["positions"]
    else:
        raise ValueError(f"Невідомий target_source: {target_source!r}")

    x_mean, x_std = X_raw.mean(axis=0), X_raw.std(axis=0) + 1e-8
    y_mean, y_std = Y_raw.mean(axis=0), Y_raw.std(axis=0) + 1e-8
    X = (X_raw - x_mean) / x_std
    Y = (Y_raw - y_mean) / y_std

    X_seq, Y_seq = [], []
    for i in range(len(X) - sequence_length):
        X_seq.append(X[i:i + sequence_length])
        Y_seq.append(Y[i + sequence_length])

    return np.array(X_seq), np.array(Y_seq), (x_mean, x_std), (y_mean, y_std)


def build_model(sequence_length, n_features):
    from tensorflow import keras
    from tensorflow.keras import layers

    model = keras.Sequential([
        layers.Input(shape=(sequence_length, n_features)),
        layers.LSTM(128, return_sequences=True),
        layers.LSTM(64),
        layers.Dense(3),
    ])
    model.compile(optimizer='adam', loss='mse')
    return model


def train(csv_path, target_source="estimator", sequence_length=DEFAULT_SEQUENCE_LENGTH,
          epochs=20, val_split=0.2, model_out=None):
    X_seq, Y_seq, x_norm, y_norm = build_dataset(csv_path, target_source, sequence_length)
    if len(X_seq) < 20:
        raise ValueError(
            f"Замало даних для навчання: {len(X_seq)} послідовностей з {csv_path} "
            f"(потрібно щонайменше ~20 при sequence_length={sequence_length})."
        )

    n_val = max(1, int(len(X_seq) * val_split))
    X_train, X_val = X_seq[:-n_val], X_seq[-n_val:]
    Y_train, Y_val = Y_seq[:-n_val], Y_seq[-n_val:]

    model = build_model(sequence_length, X_seq.shape[-1])
    history = model.fit(
        X_train, Y_train,
        validation_data=(X_val, Y_val),
        epochs=epochs, batch_size=32, verbose=1,
    )
    if model_out:
        model.save(model_out)
        print(f"Модель збережено у {model_out}")
    return model, history, x_norm, y_norm


def predict_trajectory(model, X_seq, y_norm):
    y_mean, y_std = y_norm
    preds = model.predict(X_seq, verbose=0)
    return preds * y_std + y_mean


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Тренування LSTM-апроксиматора траєкторії дрона")
    parser.add_argument("csv_path", nargs="?", default="flight_logs.csv")
    parser.add_argument(
        "--target", choices=["estimator", "local_position"], default="estimator",
        help="'estimator' = вчити на виводі InertialEstimator (є завжди); "
             "'local_position' = вчити на реальному GPS/EKF (потребує ненульових local_x/y/z у лозі)",
    )
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--sequence-length", type=int, default=DEFAULT_SEQUENCE_LENGTH)
    parser.add_argument("--model-out", default=None, help="Куди зберегти навчену модель (напр. model.keras)")
    args = parser.parse_args()

    model, history, x_norm, y_norm = train(
        args.csv_path,
        target_source=args.target,
        sequence_length=args.sequence_length,
        epochs=args.epochs,
        model_out=args.model_out,
    )
    print(f"Фінальний train loss: {history.history['loss'][-1]:.4f}")
    print(f"Фінальний val loss:   {history.history['val_loss'][-1]:.4f}")
