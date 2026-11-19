"""
Светомузыка: лента + диод на биты + парогенератор на сильные bass-drop.

Запуск:     ./run.sh  (Linux)   run.bat  (Windows)
            или python3 summary.py / python summary.py
Настройки:  settings.toml
"""

from __future__ import annotations

import argparse
import os
import random
import select
import shutil
import sys
import threading
import time
import unicodedata
from pathlib import Path

import numpy as np
import sounddevice as sd

import config
import music_led as ml
from arduino import SKETCH_VERSION, Arduino, ArduinoError, steam_byte

# --- одиночный диод (частые биты) ---
SINGLE_BEAT_K = 1.35
SINGLE_BEAT_DELTA = 0.04
SINGLE_BEAT_FLOOR = 0.035
SINGLE_REFRACTORY = 3
SINGLE_FLASH_DECAY = 0.78
SINGLE_MAX = 220

# --- туман: только самые жирные дропы (редко) ---
# порог (mult) и пауза (cooldown) — в settings.toml
FOG_BASS_HZ = (20.0, 120.0)
FOG_MIN_BASS = 0.45        # абсолютный пол после нормировки
FOG_AVG_ALPHA = 0.06       # медленный фон — обычные биты «впитываются» в avg
FOG_HISTORY = 24           # ~1.1 с локального контекста
FOG_PEAK_RATIO = 0.95      # почти локальный максимум в окне

QUIT_WORDS = ("q", "й", "exit", "quit", "выход")
IDLE_KEYS = ("f", "а")     # русские буквы — та же клавиша в русской раскладке
BRIGHT_KEYS = ("b", "и")
SPEED_KEYS = ("v", "м")
AUTO_KEYS = ("a", "ф")
SHUFFLE_KEYS = ("r", "к")
PLAY_KEYS = ("p", "з")
STEAM_KEYS = ("s", "ы")
RANDOM_WORDS = ("r", "random", "к", "случайный")


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
      • current > avg * mult
      • current > FOG_MIN_BASS
      • current ≈ локальный максимум за ~1 с
      • cooldown прошёл
    """

    def __init__(self, mult: float, cooldown: float) -> None:
        self.mult = mult
        self.cooldown = cooldown
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
        cooled = (now - self.last_fog_time) >= self.cooldown
        if not cooled or len(self.hist) < 8:
            return 0

        neighborhood = self.hist[:-1]
        local_max = float(np.max(neighborhood))
        prev = neighborhood[-1]

        is_drop = (
            current > self.avg_bass * self.mult
            and current > FOG_MIN_BASS
            and current >= local_max * FOG_PEAK_RATIO
            and current > prev + 0.08  # резкий подъём, не полка
        )

        if is_drop:
            self.last_fog_time = now
            return 1
        return 0


class Idle:
    """Фон, пока музыка не играет. Крутится в своём потоке."""

    def __init__(self, board: Arduino, mode: str, brightness: float, speed: float) -> None:
        self.board = board
        self.mode = mode
        self.brightness = brightness
        self.speed = speed
        self.last_on = mode if mode != "off" else "flow"   # что включит «f»
        self._phase = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.mode == "off" or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None

    def set_mode(self, mode: str) -> None:
        """Сменить фон на лету; off — погасить ленту."""
        if mode == "off":
            self.stop()
            self.mode = "off"
            self.board.off()
            return
        self.mode = self.last_on = mode
        self.start()

    def toggle(self) -> None:
        self.set_mode("off" if self.mode != "off" else self.last_on)

    def describe(self) -> str:
        if self.mode == "off":
            return "выкл"
        return f"{self.mode}, яркость {self.brightness:g}, скорость {self.speed:g}"

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(0.05):
            now = time.monotonic()
            self._phase += (now - last) * self.speed   # скорость меняется без скачков
            last = now
            self.board.show(ml.idle_frame(self.mode, self.brightness, self._phase))


# ---------------------------------------------------------------------------
# Треки
# ---------------------------------------------------------------------------

def _key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def list_tracks(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".mp3")


def find_track(query: str, tracks: list[Path]) -> Path:
    """Номер из меню, путь к файлу, имя или часть имени."""
    if query.isdigit():
        n = int(query)
        if 1 <= n <= len(tracks):
            return tracks[n - 1]
        raise LookupError(f"Нет трека с номером {n}.")

    for path in (Path(query).expanduser(), config.BASE / query):
        if path.is_file():
            return path

    key = _key(query)
    found = [t for t in tracks if key in (_key(t.name), _key(t.stem))]
    if not found:
        found = [t for t in tracks if key in _key(t.name)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise LookupError(f"Трек «{query}» не найден.")
    raise LookupError("Подходит несколько: " + ", ".join(t.stem for t in found))


# ---------------------------------------------------------------------------
# Воспроизведение
# ---------------------------------------------------------------------------

STOP_CHARS = "qQйЙ\r\n"   # q (и русская й) или Enter — выйти из трека в меню


class StopKey:
    """
    Ловит q / Enter во время игры без нажатия Enter после q.
    На Linux на время игры терминал переходит в режим «по одной клавише»;
    Ctrl+C работает как обычно. Остальные клавиши игнорируются.
    """

    def __init__(self) -> None:
        self._fd = None
        self._saved = None

    def __enter__(self) -> "StopKey":
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._saved is not None:
            import termios

            # tcflush — выбросить случайно нажатое, чтобы оно не попало в меню
            termios.tcsetattr(self._fd, termios.TCSAFLUSH, self._saved)
            self._saved = None

    def pressed(self) -> bool:
        if os.name == "nt":
            import msvcrt

            stop = False
            while msvcrt.kbhit():
                if msvcrt.getwch() in STOP_CHARS:
                    stop = True
            return stop

        if not select.select([sys.stdin], [], [], 0)[0]:
            return False
        if self._saved is not None:
            text = os.read(self._fd, 64).decode("utf-8", "ignore")
            return any(ch in STOP_CHARS for ch in text)
        sys.stdin.readline()   # вход не из терминала: любая строка — стоп
        return True


STOP_KEY = StopKey()


def _want_stop() -> bool:
    return STOP_KEY.pressed()


def play(board: Arduino, cfg: config.Settings, data: np.ndarray, fs: int, mode: str) -> bool:
    """True — трек доиграл, False — остановили сами (остаёмся в меню)."""
    ml.reset()
    effect = ml.EFFECTS[mode]
    normalizer = ml.VolumeNormalizer()
    beat_led = SingleLedBeat() if cfg.led_enabled else None
    fog = FogBassDropTrigger(cfg.fog_mult, cfg.fog_cooldown) if cfg.fog_enabled else None
    steam = steam_byte(cfg.fog_duration)

    current = 0
    finished = False
    stopped = False

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
        single = 0
        fog_click = 0
        if beat_led is not None or fog is not None:
            mono = chunk.mean(axis=1) if chunk.ndim == 2 else chunk
            if len(mono) >= 64:
                windowed = mono * np.hanning(len(mono))
                mag = np.abs(np.fft.rfft(windowed))
                freqs = np.fft.rfftfreq(len(mono), d=1.0 / fs)
            else:
                mag = np.zeros(2)
                freqs = np.array([0.0, fs * 0.5])
            if beat_led is not None:
                single = beat_led.update(mag, freqs)
            if fog is not None:
                fog_click = fog.update(mag, freqs)

        ml.analyze(mode, chunk, fs, cfg.sensitivity)
        volume = normalizer.process(chunk, cfg.sensitivity)
        board.show(effect(volume), single, steam if fog_click else 0)

    with sd.OutputStream(
        samplerate=fs,
        channels=data.shape[1],
        callback=callback,
        blocksize=ml.BLOCKSIZE,
    ) as stream:
        while stream.active and not finished:
            if _want_stop():
                stopped = True
                stream.stop()
                break
            time.sleep(0.1)
    return not stopped


DONE, STOPPED, FAILED = "done", "stopped", "failed"


def play_track(board: Arduino, cfg: config.Settings, path: Path, mode: str, idle: Idle) -> str:
    """DONE — доиграл, STOPPED — остановили (Enter / Ctrl+C), FAILED — не смогли."""
    print(f"Загрузка {path.name}…")
    try:
        data, fs = ml.load_mp3(str(path))
    except Exception as e:
        print(f"Не получилось открыть {path.name}: {e}")
        if shutil.which("ffmpeg") is None:
            print("Похоже, не установлен ffmpeg.")
        return FAILED

    idle.stop()
    print(f"Играет: {path.stem} | лента: {mode}")
    print("q или Enter — в меню")
    result = FAILED
    try:
        if play(board, cfg, data, fs, mode):
            print("Трек завершён")
            result = DONE
        else:
            print("Стоп")
            result = STOPPED
    except KeyboardInterrupt:
        # только стоп трека, не выход из программы
        print("\nСтоп")
        result = STOPPED
    except sd.PortAudioError as e:
        print(f"Не получилось включить звук: {e}")
    finally:
        board.off()   # лента, диод и реле — в ноль
    return result


class Playlist:
    """Что играть после трека: следующий по кругу или случайный (без повторов)."""

    def __init__(self, autoplay: bool, shuffle: bool) -> None:
        self.autoplay = autoplay
        self.shuffle = shuffle
        self._played: set[Path] = set()

    def describe(self) -> str:
        if not self.autoplay:
            return "стоп после трека"
        return "случайный трек" if self.shuffle else "следующий по кругу"

    def reset(self) -> None:
        self._played.clear()

    def first(self, tracks: list[Path]) -> Path | None:
        if not tracks:
            return None
        return random.choice(tracks) if self.shuffle else tracks[0]

    def next(self, tracks: list[Path], current: Path) -> Path | None:
        if not tracks:
            return None
        here = current.resolve()
        if not self.shuffle:
            i = next((n for n, t in enumerate(tracks) if t.resolve() == here), -1)
            return tracks[(i + 1) % len(tracks)]   # по кругу; чужой файл → первый
        self._played.add(here)
        fresh = [t for t in tracks if t.resolve() not in self._played]
        if not fresh:   # все сыграли — новый круг
            self._played = {here}
            fresh = [t for t in tracks if t.resolve() != here] or tracks
        return random.choice(fresh)


def wait_next(nxt: Path, delay: float, idle: Idle) -> bool:
    """Сообщает, что дальше. True — пора играть; False — нажали Enter / Ctrl+C."""
    print(f"Дальше: {nxt.stem} (через {delay:g} с, q или Enter — в меню)")
    if delay <= 0:
        return True
    idle.start()   # пока ждём — фон
    end = time.monotonic() + delay
    try:
        while time.monotonic() < end:
            if _want_stop():
                return False
            time.sleep(0.1)
    except KeyboardInterrupt:
        return False
    return True


def play_queue(
    board: Arduino,
    cfg: config.Settings,
    idle: Idle,
    plist: Playlist,
    path: Path,
    mode: str,
) -> str:
    """Играет трек, а потом (если включено) следующие. Возвращает, чем закончилось."""
    with STOP_KEY:   # q / Enter ловим всё время, пока играет очередь
        while True:
            result = play_track(board, cfg, path, mode, idle)
            if result != DONE or not plist.autoplay:
                return result
            nxt = plist.next(list_tracks(cfg.folder), path)
            if nxt is None:
                return result
            if not wait_next(nxt, cfg.next_delay, idle):
                return STOPPED
            path = nxt


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def idle_command(low: str, idle: Idle) -> bool:
    """
    Команды фона в меню. True — команда распознана (даже если с ошибкой).
      f            вкл/выкл        f flow   сменить вид
      b 0.3        яркость         v 2      скорость
    Русская раскладка: а = f, и = b, м = v.
    """
    words = low.split()
    if not words:
        return False
    cmd, args = words[0], words[1:]

    if cmd in IDLE_KEYS:
        if not args:
            idle.toggle()
        elif args[0] in config.IDLE_MODES:
            idle.set_mode(args[0])
        else:
            print("Фон: " + ", ".join(config.IDLE_MODES))
        return True

    if cmd in BRIGHT_KEYS or cmd in SPEED_KEYS:
        value = _number(args[0]) if args else None
        if cmd in BRIGHT_KEYS:
            if value is not None and 1 < value <= 100:
                value /= 100   # "30" = 30 %
            if value is None or not 0 <= value <= 1:
                print("Яркость: число от 0 до 1, например: b 0.3")
            else:
                idle.brightness = value
                print(f"Яркость фона {value:g}")
        else:
            if value is None or not 0.1 <= value <= 10:
                print("Скорость: число от 0.1 до 10, например: v 2")
            else:
                idle.speed = value
                print(f"Скорость фона {value:g}")
        return True

    return False


def queue_command(low: str, plist: Playlist) -> bool:
    """a — автопродолжение, r — случайный порядок (можно с on/off). Русская: ф = a, к = r."""
    words = low.split()
    if not words or words[0] not in AUTO_KEYS + SHUFFLE_KEYS:
        return False
    arg = words[1] if len(words) > 1 else None
    if arg not in (None, "on", "off", "1", "0"):
        print("После команды можно on или off, например: r on")
        return True
    value = None if arg is None else arg in ("on", "1")

    if words[0] in AUTO_KEYS:
        plist.autoplay = (not plist.autoplay) if value is None else value
    else:
        plist.shuffle = (not plist.shuffle) if value is None else value
        plist.reset()
    print(f"После трека: {plist.describe()}  (это настройка; чтобы включить музыку — p или номер трека)")
    return True


def steam_command(low: str, board: Arduino) -> bool:
    """
    s      пар идёт, а плата не знает — выключить
    s off  пар выключен, а плата думает, что идёт — запомнить, не нажимать
    Русская раскладка: ы = s.
    """
    words = low.split()
    if not words or words[0] not in STEAM_KEYS:
        return False
    if board.version < SKETCH_VERSION:
        print("Это умеет только новый скетч: залей music_led/music_led.ino заново.")
    elif len(words) > 1 and words[1] not in ("off", "on"):
        print("s — пар идёт, выключить;  s off — пар уже выключен")
    elif len(words) > 1 and words[1] == "off":
        print("Запомнила: пар выключен." if board.steam_sync(False) else "Не отправилось.")
    else:
        print("Выключаю пар…" if board.steam_sync(True) else "Не отправилось.")
    return True


def menu(board: Arduino, cfg: config.Settings, idle: Idle, plist: Playlist) -> None:
    mode = cfg.mode
    show_list = True
    while True:
        tracks = list_tracks(cfg.folder)
        idle.start()

        if show_list:
            print()
            if not tracks:
                print(f"В папке {cfg.folder.name}/ нет mp3. Положи туда треки и нажми Enter.")
            for n, track in enumerate(tracks, 1):
                print(f"  {n}. {track.stem}")
            print('Номер или часть названия, можно с режимом: "2 center"')
            print("Enter — список, Ctrl+C — выход из программы")
            print("Играть: p — первый трек (или случайный, если включён r), p r — сразу случайный")
            print("Настройки очереди (музыку сами не запускают): a — дальше сам, r — случайный порядок")
            print("Фон: f — вкл/выкл, f flow|rainbow|glow|off — вид,")
            print("     b 0.3 — яркость (0..1), v 2 — скорость (1 — обычная)")
            print("Пар: s — пар идёт, а плата не знает: выключить (s off — пар уже выключен)")
        show_list = False
        print(f"Лента: {mode} | фон: {idle.describe()} | после трека: {plist.describe()}")

        try:
            text = input("> ").strip()
        except EOFError:
            return
        low = text.casefold()

        if low in QUIT_WORDS:
            print("В меню q ничего не делает. Выход из программы — Ctrl+C.")
            continue
        if idle_command(low, idle) or queue_command(low, plist) or steam_command(low, board):
            continue

        words = text.split()
        if words and words[-1] in config.MODES:
            mode = words.pop()

        is_play = bool(words) and words[0].casefold() in PLAY_KEYS
        if is_play and len(words) == 2 and words[1].casefold() in RANDOM_WORDS:
            plist.shuffle = True   # «p r» — включить случайный порядок и сразу играть
            print("Случайный порядок: включён")
            words = words[:1]
        if is_play and len(words) == 1:
            path = plist.first(tracks)
            if path is None:
                print(f"В папке {cfg.folder.name}/ нет mp3.")
                continue
        elif not words:
            show_list = not text
            continue
        else:
            try:
                path = find_track(" ".join(words), tracks)
            except LookupError as e:
                print(e)
                continue
        plist.reset()
        play_queue(board, cfg, idle, plist, path, mode)
        show_list = True


# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Светомузыка. Без аргументов — меню треков. Настройки — settings.toml."
    )
    p.add_argument("track", nargs="?", help="номер, имя или часть имени трека")
    p.add_argument("--file", help="то же, что track")
    p.add_argument("--mode", choices=config.MODES, help="режим ленты")
    p.add_argument("--idle", choices=config.IDLE_MODES, help="фон без музыки")
    p.add_argument("--shuffle", action="store_true", help="следующий трек случайный")
    p.add_argument("--no-autoplay", action="store_true", help="не включать следующий трек сам")
    p.add_argument("--sensitivity", type=float)
    p.add_argument("--port", help="порт Arduino")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cfg = config.load()
    except config.SettingsError as e:
        print(e)
        return 1

    # аргументы командной строки важнее файла настроек
    if args.mode:
        cfg.mode = args.mode
    if args.idle:
        cfg.idle_mode = args.idle
    if args.shuffle:
        cfg.shuffle = True
    if args.no_autoplay:
        cfg.autoplay = False
    if args.sensitivity:
        cfg.sensitivity = args.sensitivity
    if args.port:
        cfg.port = args.port
    query = args.track or args.file or cfg.track

    ml.configure(cfg.num_leds, cfg.max_bright)

    track = None
    if query:
        try:
            track = find_track(query, list_tracks(cfg.folder))
        except LookupError as e:
            print(e)
            return 1

    board = None
    idle = None
    try:
        print(f"Подключаюсь к Arduino ({cfg.port})…")
        board = Arduino(cfg.port, cfg.num_leds)
        print(f"Arduino: {board.port}, диодов горит {cfg.num_leds} из {board.max_leds}")
        if board.version < SKETCH_VERSION:
            print(
                "ВНИМАНИЕ: в Arduino старый скетч, пар будет работать неправильно.\n"
                "Залей music_led/music_led.ino заново (Arduino IDE → Upload)."
            )
        idle = Idle(board, cfg.idle_mode, cfg.idle_brightness, cfg.idle_speed)
        plist = Playlist(cfg.autoplay, cfg.shuffle)
        if track is not None:
            # трек из командной строки; Enter во время игры — в меню
            if play_queue(board, cfg, idle, plist, track, cfg.mode) == STOPPED:
                menu(board, cfg, idle, plist)
        else:
            menu(board, cfg, idle, plist)
    except ArduinoError as e:
        print(e)
        return 1
    except KeyboardInterrupt:
        print("\nОстановка…")
    finally:
        # при любом выходе гасим ленту и диод, отпускаем реле
        if idle is not None:
            idle.stop()
        if board is not None:
            board.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
