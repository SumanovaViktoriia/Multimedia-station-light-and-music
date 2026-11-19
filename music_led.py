"""
Эффекты ленты и анализ звука. Запуск — summary.py, настройки — settings.toml.

Кадр эффекта: NUM_LEDS*3 байт (R,G,B). Яркость канала ≤ MAX_BRIGHT.
"""

from __future__ import annotations

import colorsys
import time
from typing import Callable

import numpy as np
from pydub import AudioSegment

# Число диодов и яркость приходят из settings.toml через configure()
NUM_LEDS = 30
MAX_BRIGHT = 100          # ~40% от 255, защита БП 2А
BLOCKSIZE = 2048          # ~46 мс звука на кадр
FRAME_BYTES = NUM_LEDS * 3
NUM_FFT_BINS = 15         # половина ленты: центр = бас, края = верх

# Состояние для эффектов, которым нужен контекст (создаётся в reset)
_frame = 0
_prev_rms = 0.0
_center_vol = 0.0
_center_radius = 0.0
_notes = None  # NoteTracker
_fft_bands = None  # FFTBandsAnalyzer
_fft_dynamic = None  # FFTDynamicAnalyzer
_fft_beat = None  # FFTBeatPulseAnalyzer

HUE_SHIFT_MULT = 0.42  # насколько громкий бин уходит в «тёплый» оттенок
BASE_HUE_SPEED = 0.028  # скорость медленного дрейфа базового hue (оборот/сек ≈)
FFT_DYNAMIC_BRIGHT = 1.22  # чуть ярче heatmap (value × множитель, всё ещё ≤ MAX_BRIGHT)

# Beat pulse: бочка vs вокал + локальное окно
# При blocksize=2048 @ 44.1kHz кадр ≈ 46 мс → 12 кадров ≈ 0.55 с контекста
BEAT_HISTORY = 12
BEAT_LOCAL_K = 1.55          # kick_now > mean(окно) * K
BEAT_DELTA = 0.055           # рост kick к прошлому кадру
BEAT_FLOOR = 0.06
BEAT_REFRACTORY = 6          # ~280 мс тишины после вспышки
# Полосы для «это удар, а не текст»
KICK_HZ = (45.0, 140.0)      # бочка / саб
VOICE_HZ = (280.0, 3200.0)   # речь / вокал
# вспышка только если скачок kick заметно сильнее скачка вокала
BEAT_KICK_OVER_VOICE = 1.75
# на куплете вокал часто громче кика — глушим вспышки
VERSE_VOICE_GATE = 0.42      # если voice выше — считаем «куплет»
VERSE_KICK_MAX = 0.85        # и kick не доминирует над voice → без вспышки
FLASH_DECAY = 0.82
IMPACT_RGB = (1.0, 0.9, 0.55)  # доли от MAX_BRIGHT


# Имена нот (pitch class 0=C … 11=B)
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


# ---------------------------------------------------------------------------
# Нормализация громкости (AGC)
# ---------------------------------------------------------------------------

class VolumeNormalizer:
    """AGC + атаки + микродинамика, чтобы лента не «замирала» на ровных кусках."""

    def __init__(self) -> None:
        self.peak = 0.03
        self.prev = 0.0
        self.beat = 0.0
        self.flux = 0.0

    def process(self, chunk: np.ndarray, sensitivity: float) -> float:
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        peak = float(np.max(np.abs(chunk)))
        energy = 0.45 * rms + 0.55 * peak

        if energy > self.peak:
            self.peak = 0.45 * self.peak + 0.55 * energy
        else:
            self.peak = 0.97 * self.peak + 0.03 * max(energy, 1e-5)

        norm = energy / (self.peak + 1e-6)
        norm = min(1.0, norm * (sensitivity / 10.0))
        punched = norm ** 0.5

        beat = 0.0
        if punched > self.prev * 1.06 + 0.015:
            beat = min(1.0, (punched - self.prev) * 5.0)
        self.beat = max(beat, self.beat * 0.4)

        delta = abs(punched - self.prev)
        self.flux = max(delta * 3.2, self.flux * 0.65)
        self.prev = 0.4 * self.prev + 0.6 * punched

        t = time.monotonic()
        # два дыхания разной частоты — меньше ощущения «стопа»
        breath = (
            0.5 + 0.5 * np.sin(t * 2.8 * 2.0 * np.pi)
            + 0.25 * np.sin(t * 5.1 * 2.0 * np.pi)
        ) / 1.25
        alive = 0.12 * breath * max(0.18, punched)

        return min(1.0, punched * 0.42 + self.beat * 0.95 + self.flux * 0.5 + alive)


# ---------------------------------------------------------------------------
# Ноты → цвет (хрома: 12 полутонов по кругу оттенков)
# ---------------------------------------------------------------------------

class NoteTracker:
    """
    Считает энергию 12 pitch class (C…B) и доминирующую ноту.
    Цвет: C=красный, дальше по радуге через квинтовый круг / полутона.
    """

    def __init__(self) -> None:
        self.chroma = np.zeros(12, dtype=np.float64)
        self.hue = 0.0
        self.note = 0  # 0=C … 11=B

    def update(self, chunk: np.ndarray, fs: int) -> None:
        mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
        if len(mono) < 64:
            return

        windowed = mono * np.hanning(len(mono))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(windowed), d=1.0 / fs)

        raw = np.zeros(12, dtype=np.float64)
        # вокал/мелодия: примерно G2…C7
        for mag, f in zip(spectrum, freqs):
            if f < 90.0 or f > 2100.0 or mag < 1e-9:
                continue
            midi = 69.0 + 12.0 * np.log2(f / 440.0)
            pc = int(np.round(midi)) % 12
            raw[pc] += float(mag)

        if raw.max() > 0:
            raw /= raw.max()
        self.chroma = 0.25 * self.chroma + 0.75 * raw

        note = int(np.argmax(self.chroma))
        self.note = note
        target_hue = note / 12.0
        d = (target_hue - self.hue + 0.5) % 1.0 - 0.5
        self.hue = (self.hue + d * 0.7) % 1.0


def note_palette() -> tuple[float, float]:
    """Центр = доминирующая нота, край = квинта (+7 полутонов)."""
    if _notes is None:
        return 0.0, 0.5
    h = _notes.hue
    return h, (h + 7.0 / 12.0) % 1.0


# ---------------------------------------------------------------------------
# FFT → 3 полосы (bass / mids / highs)
# ---------------------------------------------------------------------------

class FFTBandsAnalyzer:
    """
    Спектр через rfft, три частотных корзины со сглаживанием (attack/release).

    Математика (дискретное время, chunk длины N, частота дискретизации fs):
      X[k] = sum_{n=0}^{N-1} x[n] * exp(-2πi kn / N)   (реализация: numpy.fft.rfft)
      |X[k]| — амплитуда гармоники с частотой
      f_k = k * fs / N ,  k = 0 … N/2
      (шаг по частоте ≈ fs/N; при N=2048, fs=44100 → ≈ 21.5 Hz на бин)

    Энергия полосы = среднее |X[k]| по всем k, где f_k ∈ [f_lo, f_hi).
    Затем AGC относительно пика полосы и сглаживание: быстро вверх, медленно вниз.
    """

    def __init__(self) -> None:
        self.bass = 0.0
        self.mids = 0.0
        self.highs = 0.0
        self._peak_b = 0.05
        self._peak_m = 0.05
        self._peak_h = 0.05

    @staticmethod
    def _band_mag(mag: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        if not np.any(mask):
            return 0.0
        return float(np.mean(mag[mask]))

    @staticmethod
    def _norm_peak(peak: float, raw: float) -> tuple[float, float]:
        if raw > peak:
            peak = 0.55 * peak + 0.45 * raw
        else:
            peak = 0.985 * peak + 0.015 * max(raw, 1e-6)
        return peak, min(1.0, raw / (peak + 1e-6))

    @staticmethod
    def _smooth(env: float, value: float, attack: float = 0.55, release: float = 0.12) -> float:
        coef = attack if value > env else release
        return env + (value - env) * coef

    def update(self, chunk: np.ndarray, fs: int, sensitivity: float) -> None:
        mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
        if len(mono) < 64:
            return

        # Окно Хэннинга уменьшает утечку спектра (spectral leakage)
        n = len(mono)
        windowed = mono * np.hanning(n)
        spectrum = np.fft.rfft(windowed)
        mag = np.abs(spectrum)
        # f_k = k * fs / n
        freqs = np.fft.rfftfreq(n, d=1.0 / fs)

        # Три корзины: бас / середина / верх
        raw_b = self._band_mag(mag, freqs, 20.0, 250.0)
        raw_m = self._band_mag(mag, freqs, 250.0, 2000.0)
        raw_h = self._band_mag(mag, freqs, 2000.0, min(20000.0, fs * 0.5))

        gain = sensitivity / 10.0
        self._peak_b, nb = self._norm_peak(self._peak_b, raw_b)
        self._peak_m, nm = self._norm_peak(self._peak_m, raw_m)
        self._peak_h, nh = self._norm_peak(self._peak_h, raw_h)

        nb = min(1.0, nb * gain)
        nm = min(1.0, nm * gain)
        nh = min(1.0, nh * gain)

        # Сглаживание как у анализатора: вспышка быстро, спад медленно
        self.bass = self._smooth(self.bass, nb, attack=0.6, release=0.1)
        self.mids = self._smooth(self.mids, nm, attack=0.55, release=0.12)
        self.highs = self._smooth(self.highs, nh, attack=0.5, release=0.14)


class FFTDynamicAnalyzer:
    """
    Зеркальный heatmap-спектр: NUM_FFT_BINS лог-бинов FFT → вся лента.

    Центр ленты = низкие частоты, края = высокие.
    Цвет через colorsys HSV: медленный base_hue + сдвиг к «теплу» от амплитуды.
    Кадр RGB собирается здесь же (15 вызовов hsv_to_rgb), effect только отдаёт буфер.
    """

    def __init__(self) -> None:
        self.smoothed = np.zeros(NUM_FFT_BINS, dtype=np.float64)
        self.raw_bins = np.zeros(NUM_FFT_BINS, dtype=np.float64)  # до тяжёлого сглаживания
        self.peak = 0.05
        self.frame = bytearray(FRAME_BYTES)
        self._last_mag: np.ndarray | None = None
        self._last_freqs: np.ndarray | None = None
        # лог-границы бинов считаем лениво под fs
        self._edges: np.ndarray | None = None
        self._edges_fs = 0

    def _freq_edges(self, fs: int) -> np.ndarray:
        if self._edges is not None and self._edges_fs == fs:
            return self._edges
        f_max = min(12000.0, fs * 0.45)
        self._edges = np.logspace(np.log10(40.0), np.log10(f_max), NUM_FFT_BINS + 1)
        self._edges_fs = fs
        return self._edges

    def update(self, chunk: np.ndarray, fs: int, sensitivity: float) -> None:
        mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
        if len(mono) < 64:
            return

        n = len(mono)
        windowed = mono * np.hanning(n)
        mag = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(n, d=1.0 / fs)
        self._last_mag = mag
        self._last_freqs = freqs
        edges = self._freq_edges(fs)

        raw = np.zeros(NUM_FFT_BINS, dtype=np.float64)
        for i in range(NUM_FFT_BINS):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            if np.any(mask):
                raw[i] = float(np.mean(mag[mask]))
            else:
                # на длинной ленте бин уже шага FFT — берём значение между соседями
                raw[i] = float(np.interp((edges[i] + edges[i + 1]) / 2, freqs, mag))

        # общий пик для нормировки (heatmap выглядит ровнее)
        energy = float(raw.max()) if raw.size else 0.0
        if energy > self.peak:
            self.peak = 0.55 * self.peak + 0.45 * energy
        else:
            self.peak = 0.985 * self.peak + 0.015 * max(energy, 1e-6)

        gain = sensitivity / 10.0
        new_vals = np.clip(raw / (self.peak + 1e-6) * gain, 0.0, 1.0)
        self.raw_bins = new_vals  # для onset (ещё не «размазано»)
        # тяжёлое сглаживание — «жидкое» течение без дрожи
        self.smoothed = self.smoothed * 0.7 + new_vals * 0.3

        base_hue = (time.time() * BASE_HUE_SPEED) % 1.0
        scale = float(MAX_BRIGHT)

        for i in range(NUM_FFT_BINS):
            amp = float(self.smoothed[i])
            # тихий бин ≈ base_hue; громкий сдвигается к розово-красному
            hue = (base_hue + amp * HUE_SHIFT_MULT) % 1.0
            sat = 0.75 + 0.25 * amp
            # лёгкий подъём середины (^0.85) + общий множитель яркости
            val = min(1.0, (amp ** 0.85) * FFT_DYNAMIC_BRIGHT)
            r_f, g_f, b_f = colorsys.hsv_to_rgb(hue, sat, val)
            r = int(min(MAX_BRIGHT, round(r_f * scale)))
            g = int(min(MAX_BRIGHT, round(g_f * scale)))
            b = int(min(MAX_BRIGHT, round(b_f * scale)))

            # бин 0 → центр, последний бин → края
            left = (NUM_LEDS - 1) // 2 - i
            right = NUM_LEDS // 2 + i
            self.frame[left * 3 : left * 3 + 3] = bytes((r, g, b))
            self.frame[right * 3 : right * 3 + 3] = bytes((r, g, b))


class FFTBeatPulseAnalyzer(FFTDynamicAnalyzer):
    """
    Club mode: heatmap + удар бочки, а не вокал.

    Берём узкий kick (45–140 Hz) и voice (280–3200 Hz) из того же FFT.
    Вспышка только если:
      • kick вырос относительно локального окна (~0.5 с)
      • это локальный пик kick
      • прирост kick сильнее прироста voice  (иначе это «текст», не бит)
    """

    def __init__(self) -> None:
        super().__init__()
        self.kick_history: list[float] = []
        self.prev_kick = 0.0
        self.prev_voice = 0.0
        self.kick_peak = 0.05
        self.voice_peak = 0.05
        self.flash_intensity = 0.0
        self.refractory = 0

    @staticmethod
    def _band_mean(mag: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        if not np.any(mask):
            return 0.0
        return float(np.mean(mag[mask]))

    def update(self, chunk: np.ndarray, fs: int, sensitivity: float) -> None:
        super().update(chunk, fs, sensitivity)

        mag, freqs = self._last_mag, self._last_freqs
        if mag is None or freqs is None:
            return

        kick_raw = self._band_mean(mag, freqs, KICK_HZ[0], KICK_HZ[1])
        voice_raw = self._band_mean(mag, freqs, VOICE_HZ[0], VOICE_HZ[1])

        # отдельная нормировка полос (вокал громче кика в миксе — иначе никогда не вспыхнет)
        if kick_raw > self.kick_peak:
            self.kick_peak = 0.55 * self.kick_peak + 0.45 * kick_raw
        else:
            self.kick_peak = 0.98 * self.kick_peak + 0.02 * max(kick_raw, 1e-6)
        if voice_raw > self.voice_peak:
            self.voice_peak = 0.55 * self.voice_peak + 0.45 * voice_raw
        else:
            self.voice_peak = 0.98 * self.voice_peak + 0.02 * max(voice_raw, 1e-6)

        kick_now = min(1.0, kick_raw / (self.kick_peak + 1e-6))
        voice_now = min(1.0, voice_raw / (self.voice_peak + 1e-6))
        kick_flux = max(0.0, kick_now - self.prev_kick)
        voice_flux = max(0.0, voice_now - self.prev_voice)
        self.prev_kick = 0.55 * self.prev_kick + 0.45 * kick_now
        self.prev_voice = 0.55 * self.prev_voice + 0.45 * voice_now

        hist = self.kick_history
        hist.append(kick_now)
        if len(hist) > BEAT_HISTORY:
            del hist[0]

        beat = False
        if self.refractory > 0:
            self.refractory -= 1
        elif len(hist) >= 6:
            neighborhood = hist[:-1]
            local_mean = float(np.mean(neighborhood))
            local_max = float(np.max(neighborhood))
            prev = neighborhood[-1]

            above_local = kick_now > local_mean * BEAT_LOCAL_K + 0.02
            rising = kick_now > prev + BEAT_DELTA
            is_peak = kick_now >= local_max * 0.92
            loud_enough = kick_now > BEAT_FLOOR
            # скачок бочки сильнее скачка вокала
            drum_not_voice = kick_flux > voice_flux * BEAT_KICK_OVER_VOICE + 0.02
            # куплет: устойчивый вокал «сверху» — не мигаем
            not_verse = not (
                voice_now > VERSE_VOICE_GATE
                and kick_now < voice_now * VERSE_KICK_MAX
            )

            beat = (
                above_local
                and rising
                and is_peak
                and loud_enough
                and drum_not_voice
                and not_verse
            )

        if beat:
            self.flash_intensity = 1.0
            self.refractory = BEAT_REFRACTORY

        self.flash_intensity *= FLASH_DECAY

        if self.flash_intensity < 0.02:
            return

        half = max((NUM_LEDS - 1) / 2.0, 0.5)
        ir, ig, ib = (c * MAX_BRIGHT for c in IMPACT_RGB)
        flash = self.flash_intensity
        for i in range(NUM_LEDS):
            t = abs(i - half) / half
            local = flash * (1.0 - 0.6 * t)
            if local < 0.01:
                continue
            idx = i * 3
            br, bg, bb = self.frame[idx], self.frame[idx + 1], self.frame[idx + 2]
            self.frame[idx] = min(MAX_BRIGHT, int(br * (1.0 - local) + ir * local))
            self.frame[idx + 1] = min(MAX_BRIGHT, int(bg * (1.0 - local) + ig * local))
            self.frame[idx + 2] = min(MAX_BRIGHT, int(bb * (1.0 - local) + ib * local))



# ---------------------------------------------------------------------------
# Утилиты цвета
# ---------------------------------------------------------------------------

def clamp_byte(x: float) -> int:
    return int(max(0, min(MAX_BRIGHT, round(x))))


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """h в [0,1), s/v в [0,1]. Возвращает RGB с учётом MAX_BRIGHT."""
    h = h % 1.0
    i = int(h * 6.0) % 6
    f = h * 6.0 - int(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    scale = MAX_BRIGHT
    return clamp_byte(r * scale), clamp_byte(g * scale), clamp_byte(b * scale)


def empty_frame() -> bytearray:
    return bytearray(FRAME_BYTES)


def set_led(buf: bytearray, i: int, r: float, g: float, b: float) -> None:
    if 0 <= i < NUM_LEDS:
        buf[i * 3] = clamp_byte(r)
        buf[i * 3 + 1] = clamp_byte(g)
        buf[i * 3 + 2] = clamp_byte(b)


# ---------------------------------------------------------------------------
# Режимы визуализации (чистые по смыслу: volume → кадр)
# ---------------------------------------------------------------------------

def effect_center(rms_volume: float) -> bytearray:
    """Пульс от центра к краям. Цвет = нота; лёгкая рябь, чтобы не замирало."""
    global _center_vol, _center_radius
    buf = empty_frame()
    half = max(NUM_LEDS // 2, 1)
    target = max(0.0, min(1.0, rms_volume))
    h_center, h_edge = note_palette()
    now = time.monotonic()

    c_coef = 0.75 if target > _center_vol else 0.48
    _center_vol += (target - _center_vol) * c_coef
    r_coef = 0.9 if target > _center_radius else 0.5
    _center_radius += (target - _center_radius) * r_coef

    center_bright = 0.18 + 0.82 * (_center_vol ** 1.0)
    width = 0.2 + 1.05 * _center_radius
    wave = now * 4.6
    wave2 = now * 7.3

    for i in range(NUM_LEDS):
        t = abs(i - half) / half
        sigma = max(0.14, width * 0.9)
        fall = float(np.exp(-((t / sigma) ** 2) * 1.2))
        edge_kick = 1.0 + 1.65 * t * _center_radius
        ripple = (
            0.72
            + 0.18 * np.sin(wave + t * 6.0)
            + 0.10 * np.sin(wave2 - t * 9.0 + i * 0.35)
        )
        level = min(1.0, center_bright * fall * edge_kick * ripple)
        if level < 0.015:
            continue
        d = (h_edge - h_center + 0.5) % 1.0 - 0.5
        hue = (h_center + d * t + 0.03 * np.sin(wave * 0.55 + i * 0.08)) % 1.0
        r, g, b = hsv_to_rgb(hue, 1.0, level)
        set_led(buf, i, r, g, b)
    return buf




def effect_strobe(rms_volume: float) -> bytearray:
    """Вспышка цветом текущей ноты, иначе тусклая квинта."""
    global _prev_rms
    buf = empty_frame()
    vol = max(0.0, min(1.0, rms_volume))
    spike = vol > _prev_rms * 1.35 + 0.05
    _prev_rms = 0.85 * _prev_rms + 0.15 * vol
    h_c, h_e = note_palette()

    if vol >= 0.8 or spike:
        r, g, b = hsv_to_rgb(h_c, 1.0, 1.0)
        for i in range(NUM_LEDS):
            set_led(buf, i, r, g, b)
    elif vol > 0.05:
        r, g, b = hsv_to_rgb(h_e, 1.0, 0.12)
        for i in range(NUM_LEDS):
            set_led(buf, i, r, g, b)
    return buf


def effect_rainbow(rms_volume: float) -> bytearray:
    """Бегущая радуга со сдвигом от текущей ноты; яркость от громкости."""
    global _frame
    buf = empty_frame()
    vol = max(0.0, min(1.0, rms_volume))
    brightness = 0.15 + 0.85 * vol
    h0, _ = note_palette()

    for i in range(NUM_LEDS):
        hue = (h0 + i / NUM_LEDS + _frame * 0.02) % 1.0
        r, g, b = hsv_to_rgb(hue, 1.0, brightness)
        set_led(buf, i, r, g, b)

    _frame += 1
    return buf


def effect_fft_bands(rms_volume: float) -> bytearray:
    """
    FFT → мягкий градиент по ленте (не чистые R/G/B).

    Три полосы красят пересекающиеся «пятна» своими оттенками:
      bass  — тёплый малиново-янтарный, сильнее в центре
      mids  — мятно-бирюзовый, по бокам от центра
      highs — холодный сине-фиолетовый, на краях
    Веса — гауссовы по расстоянию от центра, зоны плавно смешиваются.
    """
    buf = empty_frame()
    if _fft_bands is None:
        return buf

    gate = 0.2 + 0.8 * max(0.0, min(1.0, rms_volume))
    bass = _fft_bands.bass * gate
    mids = _fft_bands.mids * gate
    highs = _fft_bands.highs * gate

    # Базовые цвета полос (0…1), потом × MAX_BRIGHT — без «светофора»
    c_bass = (1.00, 0.28, 0.22)   # коралл / тёплый красный
    c_mids = (0.25, 0.90, 0.55)   # мята
    c_high = (0.35, 0.40, 1.00)   # лазурь с фиолетовинкой

    half = max((NUM_LEDS - 1) / 2.0, 0.5)
    for i in range(NUM_LEDS):
        # 0 в центре → 1 на краю
        t = abs(i - half) / half

        # Перекрывающиеся веса: центр / кольцо / края
        w_b = float(np.exp(-((t / 0.42) ** 2) * 2.2))
        w_m = float(np.exp(-(((t - 0.52) / 0.28) ** 2) * 2.0))
        w_h = float(np.exp(-(((t - 1.00) / 0.38) ** 2) * 2.0))

        # Интенсивность полосы × пространственный вес
        ib = bass * w_b
        im = mids * w_m
        ih = highs * w_h

        r = (ib * c_bass[0] + im * c_mids[0] + ih * c_high[0]) * MAX_BRIGHT
        g = (ib * c_bass[1] + im * c_mids[1] + ih * c_high[1]) * MAX_BRIGHT
        b = (ib * c_bass[2] + im * c_mids[2] + ih * c_high[2]) * MAX_BRIGHT

        # Лёгкая подложка от суммы полос — лента не выглядит «дырявой»
        glow = 0.08 * (bass + mids + highs) / 3.0 * MAX_BRIGHT
        set_led(buf, i, r + glow * 0.5, g + glow * 0.35, b + glow * 0.45)
    return buf


def effect_fft_dynamic(rms_volume: float) -> bytearray:
    """Зеркальный HSV-heatmap; кадр уже собран в FFTDynamicAnalyzer.update()."""
    if _fft_dynamic is None:
        return empty_frame()
    # лёгкий гейт по общей громкости на совсем тихих кусках
    if rms_volume < 0.02:
        return empty_frame()
    return _fft_dynamic.frame


def effect_fft_beat_pulse(rms_volume: float) -> bytearray:
    """fft_dynamic + вспышка на bass onset (club mode)."""
    if _fft_beat is None:
        return empty_frame()
    if rms_volume < 0.02:
        return empty_frame()
    return _fft_beat.frame


EFFECTS: dict[str, Callable[[float], bytearray]] = {
    "center": effect_center,
    "strobe": effect_strobe,
    "rainbow": effect_rainbow,
    "fft_bands": effect_fft_bands,
    "fft_dynamic": effect_fft_dynamic,
    "fft_beat_pulse": effect_fft_beat_pulse,
}


# ---------------------------------------------------------------------------
# Фон без музыки
# ---------------------------------------------------------------------------

IDLE_MODES = ("off", "flow", "rainbow", "glow")


def idle_frame(mode: str, brightness: float, phase: float) -> bytearray:
    """
    phase — «время» фона в секундах (растёт тем быстрее, чем больше speed).
    flow    — переливы: соседние цвета плавно перетекают, вся лента медленно меняет гамму
    rainbow — бегущая радуга
    glow    — мягкое свечение одним цветом
    """
    buf = empty_frame()
    two_pi = 2.0 * np.pi
    for i in range(NUM_LEDS):
        x = i / NUM_LEDS   # 0..1 вдоль ленты, вид не зависит от числа диодов
        if mode == "flow":
            hue = (
                phase * 0.025
                + 0.16 * np.sin(two_pi * (1.3 * x) + phase * 0.55)
                + 0.09 * np.sin(two_pi * (2.7 * x) - phase * 0.35)
            )
            sat = 0.85 + 0.15 * np.sin(two_pi * x + phase * 0.3)
            val = brightness * (0.7 + 0.3 * np.sin(two_pi * (1.9 * x) - phase * 0.8))
        elif mode == "rainbow":
            hue = x + phase * 0.03
            sat = 1.0
            val = brightness
        else:
            hue = phase * 0.008 + 0.04 * np.sin(two_pi * x + phase * 0.3)
            sat = 1.0
            val = brightness * (0.75 + 0.25 * np.sin(phase * 0.9 - two_pi * x))
        r, g, b = hsv_to_rgb(hue, sat, val)
        set_led(buf, i, r, g, b)
    return buf


# ---------------------------------------------------------------------------
# Настройка / анализ / загрузка
# ---------------------------------------------------------------------------

def configure(num_leds: int, max_bright: int) -> None:
    """Длина ленты и предел яркости из settings.toml."""
    global NUM_LEDS, MAX_BRIGHT, FRAME_BYTES, NUM_FFT_BINS
    NUM_LEDS = num_leds
    MAX_BRIGHT = max_bright
    FRAME_BYTES = num_leds * 3
    NUM_FFT_BINS = (num_leds + 1) // 2
    reset()


def reset() -> None:
    """Чистое состояние эффектов перед треком."""
    global _frame, _prev_rms, _center_vol, _center_radius
    global _notes, _fft_bands, _fft_dynamic, _fft_beat
    _frame = 0
    _prev_rms = 0.0
    _center_vol = 0.0
    _center_radius = 0.0
    _notes = NoteTracker()
    _fft_bands = FFTBandsAnalyzer()
    _fft_dynamic = FFTDynamicAnalyzer()
    _fft_beat = FFTBeatPulseAnalyzer()


def analyze(mode: str, chunk: np.ndarray, fs: int, sensitivity: float) -> None:
    """Тяжёлый анализ — только тот, что нужен режиму."""
    if mode == "fft_bands":
        _fft_bands.update(chunk, fs, sensitivity)
    elif mode == "fft_dynamic":
        _fft_dynamic.update(chunk, fs, sensitivity)
    elif mode == "fft_beat_pulse":
        _fft_beat.update(chunk, fs, sensitivity)
    else:
        _notes.update(chunk, fs)


def load_mp3(path: str) -> tuple[np.ndarray, int]:
    """MP3 → float32 массив shape (N, channels), samplerate."""
    audio = AudioSegment.from_mp3(path)
    samples = np.array(audio.get_array_of_samples())
    if audio.channels == 2:
        samples = samples.reshape((-1, 2))
    else:
        samples = samples.reshape((-1, 1))
    max_amp = 2 ** (8 * audio.sample_width - 1)
    data = samples.astype(np.float32) / max_amp
    return data, int(audio.frame_rate)

