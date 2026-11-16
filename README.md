# Светомузыка + туман

Для шоу с **парогенератором, диодом и лентой** — скрипт `summary.py`  
(не `music_led.py`: там только лента, без тумана).

```bash
source .venv/bin/activate
python3 summary.py --mode <режим> --file трек.mp3
```

`--file` можно без `music/` — папка подставится сама.  
Режимы ленты в `summary.py`: `rainbow`, `center`.  
Диод мигает на биты, туман — на сильные bass-drop.

## Примеры

Кукла Колдуна + радуга (+ диод + туман):
```bash
python3 summary.py --mode rainbow --file КиШ_Кукла_Колдуна.mp3
```

Сурганова + центр (+ диод + туман):
```bash
python3 summary.py --mode center --file Сурганова_Перрон.mp3
```

Только лента без тумана — `music_led.py` (там больше режимов: strobe, fft_* и т.д.).
