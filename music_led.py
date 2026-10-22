# source .venv/bin/activate

import sounddevice as sd
import numpy as np
import serial
import time
from pydub import AudioSegment

# --- НАСТРОЙКИ ---
PORT = '/dev/ttyACM0'
BAUD = 115200
NUM_LEDS = 30
MUSIC_FILE = 'music/Сурганова_Перрон.mp3'
BRIGHTNESS = 0.45
SENSITIVITY = 1.6
LED_FPS = 45
# -----------------

try:
    # write_timeout=0: если буфер USB забит — кадр пропускаем, звук не зависает
    ser = serial.Serial(PORT, BAUD, write_timeout=0)
    time.sleep(2)
    print("Успешно подключено к Arduino!")
except Exception as e:
    print(f"Ошибка подключения к {PORT}: {e}")
    exit()

try:
    print(f"Распаковка файла {MUSIC_FILE} (может занять пару секунд)...")
    audio = AudioSegment.from_mp3(MUSIC_FILE)
    fs = audio.frame_rate

    samples = np.array(audio.get_array_of_samples())
    if audio.channels == 2:
        samples = samples.reshape((-1, 2))
    else:
        samples = samples.reshape((-1, 1))

    max_amp = 2 ** (8 * audio.sample_width - 1)
    data = samples.astype(np.float32) / max_amp
    print("Файл загружен!")
except Exception as e:
    print(f"Ошибка чтения MP3: {e}")
    exit()

current_frame = 0
finished = False
last_led_t = 0.0
led_interval = 1.0 / LED_FPS

# по одному уровню на каждый светодиод (спектр вдоль всей ленты)
band_env = np.zeros(NUM_LEDS, dtype=np.float64)
beat_flash = 0.0
prev_bass = 0.0
prev_overall = 0.0

# логарифмические границы частот: слева бас → справа верх
F_MIN, F_MAX = 40.0, 8000.0
_edges = np.logspace(np.log10(F_MIN), np.log10(F_MAX), NUM_LEDS + 1)


def smooth(env, value, attack=0.75, release=0.28):
    """Быстрый подъём и довольно быстрый спад — заметнее мигание."""
    coef = attack if value > env else release
    return env + (value - env) * coef


def analyze_chunk(chunk):
    """Спектр по всей ленте + вспышки на скачки громкости/баса."""
    global band_env, beat_flash, prev_bass, prev_overall

    mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
    if len(mono) < 16:
        return band_env, beat_flash

    windowed = mono * np.hanning(len(mono))
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(windowed), d=1.0 / fs)

    raw = np.zeros(NUM_LEDS, dtype=np.float64)
    for i in range(NUM_LEDS):
        lo, hi = _edges[i], _edges[i + 1]
        mask = (freqs >= lo) & (freqs < hi)
        if np.any(mask):
            gain = 0.005 * SENSITIVITY * (1.0 + 0.9 * i / NUM_LEDS)
            raw[i] = min(1.0, float(np.sqrt(np.mean(spectrum[mask] ** 2))) * gain)

    for i in range(NUM_LEDS):
        if raw[i] > 0:
            continue
        left = raw[i - 1] if i > 0 else 0.0
        right = raw[i + 1] if i + 1 < NUM_LEDS else 0.0
        if left or right:
            raw[i] = 0.5 * (left + right)

    # вся лента всегда чуть живёт от общей громкости
    overall = min(1.0, float(np.sqrt(np.mean(mono ** 2))) * 4.2 * SENSITIVITY)
    raw = np.clip(raw * 0.8 + overall * 0.28, 0.0, 1.0)

    bass_raw = float(np.mean(raw[: max(3, NUM_LEDS // 6)]))
    beat = 0.0
    # удар по басу
    if bass_raw > 0.07 and bass_raw > prev_bass * 1.2 + 0.025:
        beat = max(beat, min(1.0, (bass_raw - prev_bass) * 3.2))
    # скачок общей громкости (вокал, хлопки) — мигает вся лента
    if overall > 0.06 and overall > prev_overall * 1.18 + 0.02:
        beat = max(beat, min(1.0, (overall - prev_overall) * 2.8))

    prev_bass = smooth(prev_bass, bass_raw, attack=0.65, release=0.25)
    prev_overall = smooth(prev_overall, overall, attack=0.65, release=0.25)
    beat_flash = max(beat, beat_flash * 0.55)  # вспышка коротко гаснет → резче мигание

    for i in range(NUM_LEDS):
        band_env[i] = smooth(band_env[i], raw[i])

    return band_env, beat_flash


def hsv_to_rgb(h, s, v):
    """Чистый HSV → RGB. s=1 даёт только насыщенные цвета, без белого."""
    i = int(h * 6.0) % 6
    f = h * 6.0 - int(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t_ = v * (1.0 - (1.0 - f) * s)
    if i == 0:
        r, g, b = v, t_, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t_
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t_, p, v
    else:
        r, g, b = v, p, q
    return int(r * 255), int(g * 255), int(b * 255)


def build_leds(levels, beat):
    """Спектр: слева красный → зелёный → синий. Насыщенность всегда 100%."""
    led_data = bytearray(NUM_LEDS * 3)
    beat = min(1.0, beat * 0.7)

    for i in range(NUM_LEDS):
        t = i / max(NUM_LEDS - 1, 1)
        level = float(levels[i])
        level = max(0.0, min(1.0, level ** 0.75))
        # только яркость цвета растёт, оттенок не белеет
        value = max(0.0, min(1.0, level * (1.0 + beat * 0.9) + beat * 0.12))

        # 0 = red, ~0.33 = green, ~0.66 = blue (не доходим до пурпура/белых смесей)
        hue = t * 0.66
        r, g, b = hsv_to_rgb(hue, 1.0, value)

        led_data[i * 3] = int(r * BRIGHTNESS)
        led_data[i * 3 + 1] = int(g * BRIGHTNESS)
        led_data[i * 3 + 2] = int(b * BRIGHTNESS)

    return led_data










def send_leds(led_data):
    try:
        ser.write(b'LED')
        ser.write(led_data)
    except (serial.SerialTimeoutException, serial.SerialException, OSError):
        # буфер забит или порт моргнул — этот кадр пропускаем
        try:
            ser.reset_output_buffer()
        except Exception:
            pass


def audio_callback(outdata, frames, time_info, status):
    global current_frame, finished, last_led_t

    chunk = data[current_frame : current_frame + frames]

    if len(chunk) < frames:
        outdata[: len(chunk)] = chunk
        outdata[len(chunk) :] = 0
        current_frame += len(chunk)
        finished = True
        raise sd.CallbackStop()

    outdata[:] = chunk
    current_frame += frames

    now = time.monotonic()
    if now - last_led_t < led_interval:
        return
    last_led_t = now

    levels, beat = analyze_chunk(chunk)
    send_leds(build_leds(levels, beat))


print("Начинаю воспроизведение... Нажми Ctrl+C для выхода.")
try:
    with sd.OutputStream(samplerate=fs, channels=data.shape[1], callback=audio_callback):
        while not finished and current_frame < len(data):
            time.sleep(0.1)
    print("Трек завершен!")
except KeyboardInterrupt:
    print("\nОстановка...")

try:
    ser.write_timeout = 1
    ser.write(b'LED' + bytearray([0] * (NUM_LEDS * 3)))
    ser.close()
except Exception:
    pass
