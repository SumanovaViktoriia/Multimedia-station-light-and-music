"""Чтение settings.toml."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 и старше
    import tomli as tomllib

from music_led import EFFECTS, IDLE_MODES

BASE = Path(__file__).resolve().parent
SETTINGS_FILE = BASE / "settings.toml"

MODES = tuple(EFFECTS)

# Значения, если в файле чего-то нет
DEFAULTS = {
    "system": {"os": "auto", "port_linux": "/dev/ttyACM0", "port_windows": "COM5"},
    "strip": {"num_leds": 30, "max_bright": 100},
    "music": {
        "folder": "music",
        "track": "",
        "mode": "rainbow",
        "sensitivity": 10.0,
        "autoplay": True,
        "shuffle": False,
        "next_delay": 5.0,
    },
    "led": {"enabled": True},
    "fog": {"enabled": True, "mult": 2.8, "cooldown": 8.0, "duration": 4.0},
    "idle": {"mode": "flow", "brightness": 0.15, "speed": 1.0},
}


class SettingsError(Exception):
    pass


@dataclass
class Settings:
    port: str
    num_leds: int
    max_bright: int
    folder: Path
    track: str
    mode: str
    sensitivity: float
    autoplay: bool
    shuffle: bool
    next_delay: float
    led_enabled: bool
    fog_enabled: bool
    fog_mult: float
    fog_cooldown: float
    fog_duration: float
    idle_mode: str
    idle_brightness: float
    idle_speed: float


def _check(ok: bool, text: str) -> None:
    if not ok:
        raise SettingsError(f"settings.toml: {text}")


def _number(value, name: str, lo: float, hi: float) -> float:
    is_num = isinstance(value, (int, float)) and not isinstance(value, bool)
    _check(is_num and lo <= value <= hi, f"{name} должно быть числом от {lo:g} до {hi:g}")
    return float(value)


def _whole(value, name: str, lo: int, hi: int) -> int:
    is_int = isinstance(value, int) and not isinstance(value, bool)
    _check(is_int and lo <= value <= hi, f"{name} должно быть целым от {lo} до {hi}")
    return value


def _choice(value, name: str, options) -> str:
    _check(value in options, f"{name} = {value!r}, а можно: {', '.join(options)}")
    return value


def load(path: Path = SETTINGS_FILE) -> Settings:
    raw: dict = {}
    if path.is_file():
        try:
            # utf-8-sig: Блокнот на Windows может дописать BOM в начало файла
            raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise SettingsError(f"settings.toml не читается: {e}") from None
    else:
        print(f"Нет файла {path.name}, беру настройки по умолчанию.")

    cfg = {}
    for section, defaults in DEFAULTS.items():
        user = raw.get(section, {})
        _check(isinstance(user, dict), f"[{section}] должен быть разделом")
        for key in user:
            _check(key in defaults, f"неизвестная настройка {key} в [{section}]")
        cfg[section] = {**defaults, **user}
    for section in raw:
        _check(section in DEFAULTS, f"неизвестный раздел [{section}]")

    system = _choice(cfg["system"]["os"], "os", ("auto", "linux", "windows"))
    if system == "auto":
        system = "windows" if platform.system() == "Windows" else "linux"
    port = cfg["system"][f"port_{system}"]
    _check(isinstance(port, str) and port != "", f"port_{system} должен быть строкой")

    for name in ("folder", "track"):
        _check(isinstance(cfg["music"][name], str), f"{name} должно быть строкой в кавычках")
    for name in ("autoplay", "shuffle"):
        _check(isinstance(cfg["music"][name], bool), f"{name} должно быть true или false")
    for section in ("led", "fog"):
        _check(
            isinstance(cfg[section]["enabled"], bool),
            f"enabled в [{section}] должно быть true или false",
        )

    return Settings(
        port=port,
        num_leds=_whole(cfg["strip"]["num_leds"], "num_leds", 1, 250),
        max_bright=_whole(cfg["strip"]["max_bright"], "max_bright", 1, 255),
        folder=BASE / cfg["music"]["folder"],
        track=cfg["music"]["track"].strip(),
        mode=_choice(cfg["music"]["mode"], "mode в [music]", MODES),
        sensitivity=_number(cfg["music"]["sensitivity"], "sensitivity", 0.1, 100),
        autoplay=cfg["music"]["autoplay"],
        shuffle=cfg["music"]["shuffle"],
        next_delay=_number(cfg["music"]["next_delay"], "next_delay", 0, 60),
        led_enabled=cfg["led"]["enabled"],
        fog_enabled=cfg["fog"]["enabled"],
        fog_mult=_number(cfg["fog"]["mult"], "mult в [fog]", 1, 100),
        fog_cooldown=_number(cfg["fog"]["cooldown"], "cooldown в [fog]", 0, 3600),
        fog_duration=_number(cfg["fog"]["duration"], "duration в [fog]", 0.1, 20),
        idle_mode=_choice(cfg["idle"]["mode"], "mode в [idle]", IDLE_MODES),
        idle_brightness=_number(cfg["idle"]["brightness"], "brightness в [idle]", 0, 1),
        idle_speed=_number(cfg["idle"]["speed"], "speed в [idle]", 0.1, 10),
    )
