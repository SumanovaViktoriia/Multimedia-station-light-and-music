# source .venv/bin/activate
# python3 first_script_with_svetodiod.py --file КиШ_Кукла_Колдуна.mp3
#
# Лента: режим rainbow.
# Обычный светодиод на D5: мигает на удары (бочка).
# Протокол Arduino: b'LED' + 90 байт RGB + 1 байт яркости (0–255) для analogWrite.

from __future__ import annotations

import argparse
import time

import numpy as np
import serial
import sounddevice as sd

import music_led as ml

# Чуть злее ловим удары для одиночного диода
SINGLE_BEAT_K = 1.35
SINGLE_BEAT_DELTA = 0.04
SINGLE_BEAT_FLOOR = 0.035
SINGLE_REFRACTORY = 3
SINGLE_FLASH_DECAY = 0.78
SINGLE_MAX = 220


class SingleLedBeat:
    """Onset по kick из своего маленького FFT — для диода на D5."""

    def __init__(self) -> None:
        self.hist: list[float] = []
        self.peak = 0.05
        self.flash = 0.0
        self.refractory = 0

    def update(self, chunk: np.ndarray, fs: int) -> int:
        mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
        if len(mono) < 64:
            self.flash *= SINGLE_FLASH_DECAY
            return int(min(SINGLE_MAX, round(self.flash * SINGLE_MAX)))

        windowed = mono * np.hanning(len(mono))
        mag = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(mono), d=1.0 / fs)
        mask = (freqs >= ml.KICK_HZ[0]) & (freqs < ml.KICK_HZ[1])
        raw = float(np.mean(mag[mask])) if np.any(mask) else 0.0

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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Лента rainbow + одиночный светодиод на биты"
    )
    p.add_argument("--file", default="music/track.mp3")
    p.add_argument("--sensitivity", type=float, default=10.0)
    p.add_argument("--port", default=ml.PORT)
    return p.parse_args()


def send_frame(ser: serial.Serial, led_data: bytearray, single: int) -> None:
    try:
        ser.write(b"LED")
        ser.write(led_data)
        ser.write(bytes([max(0, min(255, single))]))
    except (serial.SerialTimeoutException, serial.SerialException, OSError):
        try:
            ser.reset_output_buffer()
        except Exception:
            pass


def blackout(ser: serial.Serial) -> None:
    try:
        ser.write_timeout = 1
        ser.write(b"LED" + bytearray([0] * (ml.FRAME_BYTES + 1)))
    except Exception:
        pass


def main() -> None:
    args = parse_args()

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
        print(f"OK: лента=rainbow, одиночный диод=биты, {fs} Hz")
    except Exception as e:
        print(f"Ошибка MP3: {e}")
        ser.close()
        return

    # rainbow берёт цвет из нот
    ml._notes = ml.NoteTracker()
    beat_led = SingleLedBeat()
    normalizer = ml.VolumeNormalizer()

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

        ml._notes.update(chunk, fs)
        volume = normalizer.process(chunk, args.sensitivity)
        led_data = ml.effect_rainbow(volume)
        single = beat_led.update(chunk, fs)
        send_frame(ser, led_data, single)

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
