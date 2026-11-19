"""
Связь с Arduino по USB.

Кадр: b"LED" + N + N*3 байт RGB + диод + пар + XOR всех байт после "LED".
Скетч показывает кадр, только если XOR сошёлся.
Пар: 0 — ничего, 1..200 — включить на N/10 с (плата сама выключит), 201 — выключить.
"""

from __future__ import annotations

import threading
import time

import serial
from serial.tools import list_ports

BAUD = 115200
ASK_HELLO = b"LED\xff"
SKETCH_VERSION = 4   # PROTOCOL в скетче; 4 — помнит пар между запусками, есть сверка


class ArduinoError(Exception):
    pass


STEAM_OFF = 201      # выключить пар сейчас
STEAM_IS_ON = 202    # «пар идёт» (плата не знала): выключить
STEAM_IS_OFF = 203   # «пар выключен» (плата думала, что идёт): запомнить
STEAM_MAX_SEC = 20.0


def steam_byte(seconds: float) -> int:
    """Секунды пара → байт кадра (шаг 0.1 с)."""
    return max(1, min(200, round(seconds * 10)))


def pack(rgb: bytes, single: int = 0, fog: int = 0) -> bytes:
    """Собрать кадр."""
    body = bytes([len(rgb) // 3]) + rgb + bytes([max(0, min(255, single)), max(0, min(255, int(fog)))])
    crc = 0
    for b in body:
        crc ^= b
    return b"LED" + body + bytes([crc])


def ports_hint() -> str:
    ports = [f"{p.device} ({p.description})" for p in list_ports.comports()]
    if not ports:
        return "Портов не видно: проверь USB-кабель."
    return "Доступные порты: " + ", ".join(ports)


def find_port() -> str:
    """port = "auto": берём единственное USB-устройство."""
    usb = [p.device for p in list_ports.comports() if p.vid is not None]
    if len(usb) == 1:
        return usb[0]
    raise ArduinoError(
        "Не получилось выбрать порт сам, впиши его в settings.toml.\n" + ports_hint()
    )


class Arduino:
    def __init__(self, port: str, num_leds: int) -> None:
        if port == "auto":
            port = find_port()
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0.2, write_timeout=1)
        except (serial.SerialException, OSError) as e:
            raise ArduinoError(f"Не открылся порт {port}: {e}\n{ports_hint()}") from None

        self.port = port
        self.num_leds = num_leds
        try:
            self.max_leds, self.version = self._hello()
            if num_leds > self.max_leds:
                raise ArduinoError(
                    f"В настройках num_leds = {num_leds}, а в скетче MAX_LEDS = {self.max_leds}.\n"
                    "Увеличь MAX_LEDS в music_led/music_led.ino и перезалей скетч."
                )
        except (serial.SerialException, OSError) as e:
            self.ser.close()
            raise ArduinoError(f"Порт {port} открылся, но связь оборвалась: {e}") from None
        except BaseException:
            self.ser.close()
            raise

        # пауза между кадрами: время кадра в проводе + вывод на ленту
        frame_len = num_leds * 3 + 7
        self._gap = frame_len * 10 / BAUD + self.max_leds * 35e-6 + 0.004

        self._next: tuple[bytes, int] | None = None
        self._fog = 0
        self._lock = threading.Lock()        # очередь из одного кадра
        self._write_lock = threading.Lock()  # запись в порт
        self._wake = threading.Event()
        self._closing = False
        self._broken = False
        self._thread = threading.Thread(target=self._sender, daemon=True)
        self._thread.start()

    def _hello(self) -> tuple[int, int]:
        """
        Открытие порта перезагружает Uno, после старта она пишет READY <MAX_LEDS> <версия>.
        Старый скетч версии не пишет — считаем её единицей.
        """
        for ask, wait in ((False, 3.0), (True, 1.5)):
            if ask:
                self.ser.write(ASK_HELLO)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                words = self.ser.readline().decode("ascii", "ignore").split()
                if len(words) in (2, 3) and words[0] == "READY" and all(w.isdigit() for w in words[1:]):
                    return int(words[1]), int(words[2]) if len(words) == 3 else 1
        raise ArduinoError(
            f"Arduino на {self.port} не отвечает.\n"
            "Залей в неё скетч music_led/music_led.ino (и закрой монитор порта в Arduino IDE)."
        )

    def show(self, rgb: bytes, single: int = 0, fog: int = 0) -> None:
        """
        Отдать кадр на отправку. Не ждёт порт, можно звать из аудио-колбэка.
        fog — байт пара (steam_byte(секунды)); 0 — ничего.
        """
        with self._lock:
            self._next = (bytes(rgb), single)
            self._fog = max(self._fog, fog)   # команду пара не теряем, даже если кадр пропущен
        self._wake.set()

    def _write(self, frame: bytes) -> bool:
        try:
            self.ser.write(frame)
            return True
        except serial.SerialTimeoutException:
            try:
                self.ser.reset_output_buffer()
            except Exception:
                pass
        except (serial.SerialException, OSError):
            if not self._broken:
                self._broken = True
                print("\nArduino отключилась, свет больше не управляется.")
        return False

    def _sender(self) -> None:
        """Шлёт самый свежий кадр; если не успеваем — старые пропускаем."""
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._closing:
                return
            with self._write_lock:
                with self._lock:
                    item, self._next = self._next, None
                    fog, self._fog = self._fog, 0
                if item is None:
                    continue
                self._write(pack(item[0], item[1], fog))
            time.sleep(self._gap)

    def off(self) -> None:
        """Погасить ленту и диод, выключить пар."""
        zero = pack(b"", 0, STEAM_OFF)   # N = 0: скетч гасит всю ленту
        with self._write_lock:
            with self._lock:
                self._next = None
                self._fog = 0
            # три раза: если Arduino была посреди битого кадра, первый может пропасть
            for _ in range(3):
                if not self._write(zero):
                    break
                try:
                    self.ser.flush()
                except Exception:
                    break
                time.sleep(0.03)

    def steam_sync(self, running: bool) -> bool:
        """
        Сказать плате, что на самом деле с паром.
        running=True — пар идёт, плата выключит; False — пар выключен, плата запомнит.
        Команду шлём один раз: повтор «пар идёт» мог бы пар снова включить.
        """
        frame = pack(b"", 0, STEAM_IS_ON if running else STEAM_IS_OFF)
        with self._write_lock:
            with self._lock:
                self._next = None   # не даём устаревшему кадру пойти следом
            ok = self._write(frame)
            try:
                self.ser.flush()
            except Exception:
                ok = False
        return ok

    def close(self) -> None:
        self._closing = True
        self._wake.set()
        self._thread.join(timeout=2)
        self.off()
        try:
            self.ser.close()
        except Exception:
            pass
