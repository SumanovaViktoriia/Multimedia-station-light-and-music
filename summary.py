# source .venv/bin/activate
# python3 summary.py --mode rainbow --file КиШ_Кукла_Колдуна.mp3
#
# Лента: --mode rainbow | center
# Диод D5: мигает на kick-биты
# Парогенератор (реле D7): клик на сильные bass-drop'ы
# Протокол: b'LED' + 90 RGB + [диод 0..255] + [fog 0/1]

from __future__ import annotations

import argparse
import time

import numpy as np
import serial
import sounddevice as sd

import music_led as ml

# --- одиночный диод (частые биты) ---
SINGLE_BEAT_K = 1.35
SINGLE_BEAT_DELTA = 0.04
SINGLE_BEAT_FLOOR = 0.035
SINGLE_REFRACTORY = 3
SINGLE_FLASH_DECAY = 0.78
SINGLE_MAX = 220

# --- туман: только самые жирные дропы (редко) ---
FOG_BASS_HZ = (20.0, 120.0)
FOG_ONSET_MULT = 2.8       # current > avg * MULT  (выше → реже)
FOG_MIN_BASS = 0.45        # абсолютный пол после нормировки
FOG_AVG_ALPHA = 0.06       # медленный фон — обычные биты «впитываются» в avg
FOG_COOLDOWN_SEC = 8.0     # минимум между кликами реле
FOG_HISTORY = 24           # ~1.1 с локального контекста
FOG_PEAK_RATIO = 0.95      # почти локальный максимум в окне


def bass_energy(mag: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return 0.0
    return float(np.mean(mag[mask]))


class SingleLedBeat:
    """Частые вспышки диода на kick."""

    def __init__(self) -> None:
        self.hist: list[float] = []
        self.peak = 0.05
        self.flash = 0.0
        self.refractory = 0

    def update(self, mag: np.ndarray, freqs: np.ndarray) -> int:
        raw = bass_energy(mag, freqs, ml.KICK_HZ[0], ml.KICK_HZ[1])
        if raw > self.peak:
            self.peak = 0.5 * self.peak + 0.5 * raw
        else:
            self.peak = 0.97 * self.peak + 0.03 * max(raw, 1e-6)

        now = min(1.0, raw / (self.peak + 1e-6))
        self.hist.append(now)
        if len(self.hist) > ml.BEAT_HISTORY:
            del self.hist[0]

        beat = False
        if self.refractory > 0:
            self.refractory -= 1
        elif len(self.hist) >= 5:
            neigh = self.hist[:-1]
            local_mean = float(np.mean(neigh))
            local_max = float(np.max(neigh))
            prev = neigh[-1]
            beat = (
                now > local_mean * SINGLE_BEAT_K + 0.015
                and now > prev + SINGLE_BEAT_DELTA
                and now >= local_max * 0.88
                and now > SINGLE_BEAT_FLOOR
            )

        if beat:
            self.flash = 1.0
            self.refractory = SINGLE_REFRACTORY

        self.flash *= SINGLE_FLASH_DECAY
        return int(min(SINGLE_MAX, round(self.flash * SINGLE_MAX)))


class FogBassDropTrigger:
    """
    Туман только на редких сильных bass-drop'ах.

    Нужно сразу:
      • current > avg * FOG_ONSET_MULT
      • current > FOG_MIN_BASS
      • current ≈ локальный максимум за ~1 с
      • cooldown прошёл
    """

    def __init__(self) -> None:
        self.avg_bass = 0.08
        self.peak = 0.12
        self.hist: list[float] = []
        self.last_fog_time = 0.0

    def update(self, mag: np.ndarray, freqs: np.ndarray) -> int:
        raw = bass_energy(mag, freqs, FOG_BASS_HZ[0], FOG_BASS_HZ[1])

        if raw > self.peak:
            self.peak = 0.6 * self.peak + 0.4 * raw
        else:
            self.peak = 0.997 * self.peak + 0.003 * max(raw, 1e-6)

        current = min(1.0, raw / (self.peak + 1e-6))
        self.avg_bass = (
            self.avg_bass * (1.0 - FOG_AVG_ALPHA) + current * FOG_AVG_ALPHA
        )

        self.hist.append(current)
        if len(self.hist) > FOG_HISTORY:
            del self.hist[0]

        now = time.time()
        cooled = (now - self.last_fog_time) >= FOG_COOLDOWN_SEC
        if not cooled or len(self.hist) < 8:
            return 0

        neighborhood = self.hist[:-1]
        local_max = float(np.max(neighborhood))
        prev = neighborhood[-1]

        is_drop = (
            current > self.avg_bass * FOG_ONSET_MULT
            and current > FOG_MIN_BASS
            and current >= local_max * FOG_PEAK_RATIO
            and current > prev + 0.08  # резкий подъём, не полка
        )

        if is_drop:
            self.last_fog_time = now
            return 1
        return 0



STRIP_MODES = ("rainbow", "center")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Лента + диод на биты + туман на сильные bass-drop"
    )
    p.add_argument(
        "--mode",
        choices=STRIP_MODES,
        default="rainbow",
        help="Режим ленты: rainbow или center",
    )
    p.add_argument("--file", default="music/track.mp3")
    p.add_argument("--sensitivity", type=float, default=10.0)
    p.add_argument("--port", default=ml.PORT)
    p.add_argument(
        "--fog-mult",
        type=float,
        default=FOG_ONSET_MULT,
        help="Порог дропа: current > avg * mult (больше → реже туман)",
    )
    p.add_argument(
        "--fog-cooldown",
        type=float,
        default=FOG_COOLDOWN_SEC,
        help="Минимум секунд между кликами реле",
    )
    return p.parse_args()


def send_frame(
    ser: serial.Serial, led_data: bytearray, single: int, fog: int
) -> None:
    """LED + 90 RGB + диод + fog_click."""
    try:
        ser.write(b"LED")
        ser.write(led_data)
        ser.write(bytes([max(0, min(255, single)), 1 if fog else 0]))
    except (serial.SerialTimeoutException, serial.SerialException, OSError):
        try:
            ser.reset_output_buffer()
        except Exception:
            pass


def blackout(ser: serial.Serial) -> None:
    try:
        ser.write_timeout = 1
        ser.write(b"LED" + bytearray([0] * (ml.FRAME_BYTES + 2)))
    except Exception:
        pass


def main() -> None:
    global FOG_ONSET_MULT, FOG_COOLDOWN_SEC
    args = parse_args()
    FOG_ONSET_MULT = args.fog_mult
    FOG_COOLDOWN_SEC = args.fog_cooldown

    try:
        ser = serial.Serial(args.port, ml.BAUD, write_timeout=0)
        time.sleep(2)
        print(f"Arduino: {args.port} @ {ml.BAUD}")
    except Exception as e:
        print(f"Не удалось открыть {args.port}: {e}")
        return

    try:
        path = ml.resolve_music_path(args.file)
        print(f"Загрузка {path}...")
        data, fs = ml.load_mp3(path)
        print(
            f"OK: лента={args.mode} + диод(биты) + туман(bass-drop×{FOG_ONSET_MULT}, "
            f"cooldown {FOG_COOLDOWN_SEC}s), {fs} Hz"
        )
    except Exception as e:
        print(f"Ошибка MP3: {e}")
        ser.close()
        return

    ml._notes = ml.NoteTracker()
    beat_led = SingleLedBeat()
    fog = FogBassDropTrigger()
    normalizer = ml.VolumeNormalizer()
    strip_effect = (
        ml.effect_center if args.mode == "center" else ml.effect_rainbow
    )

    current = 0
    finished = False

    def callback(outdata, frames, time_info, status):
        nonlocal current, finished

        chunk = data[current : current + frames]
        if len(chunk) < frames:
            outdata[: len(chunk)] = chunk
            outdata[len(chunk) :] = 0
            current += len(chunk)
            finished = True
            raise sd.CallbackStop()

        outdata[:] = chunk
        current += frames

        # один FFT на кадр — и диод, и туман
        mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
        if len(mono) >= 64:
            windowed = mono * np.hanning(len(mono))
            mag = np.abs(np.fft.rfft(windowed))
            freqs = np.fft.rfftfreq(len(mono), d=1.0 / fs)
        else:
            mag = np.zeros(2)
            freqs = np.array([0.0, fs * 0.5])

        ml._notes.update(chunk, fs)
        volume = normalizer.process(chunk, args.sensitivity)
        led_data = strip_effect(volume)

        single = beat_led.update(mag, freqs)
        fog_click = fog.update(mag, freqs)
        send_frame(ser, led_data, single, fog_click)

    print("Воспроизведение… Ctrl+C — стоп")
    try:
        with sd.OutputStream(
            samplerate=fs,
            channels=data.shape[1],
            callback=callback,
            blocksize=ml.BLOCKSIZE,
        ):
            while not finished and current < len(data):
                time.sleep(0.1)
        print("Трек завершён")
    except KeyboardInterrupt:
        print("\nОстановка…")
    finally:
        blackout(ser)
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
