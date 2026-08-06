# Установка на Mac и Linux

То же самое, что на Windows, только вместо двойных кликов — четыре команды в
терминале. Скрипты лежат в папке `mac-linux/`.

Перед этим нужно пройти шаг 1 — бот и группа в Telegram. Он в
[«Как поставить себе»](../README.md#как-поставить-себе), раскрывашка «Шаг 1».

## Проверь Python

```bash
python3 -V
```

Нужен **3.11 или новее**.

- **macOS**: если Python нет или он старый — `brew install python@3.12`, либо
  скачай установщик с [python.org](https://www.python.org/downloads/).
- **Ubuntu/Debian**: `sudo apt install python3 python3-venv`. Пакет
  `python3-venv` нужен обязательно, без него установка сорвётся.

## Скачай мост

```bash
git clone https://github.com/dimaad5017-dotcom/max-tg-bridge.git
cd max-tg-bridge
```

Без git можно кнопкой **Code → Download ZIP** и распаковать куда удобно.

## Четыре шага по порядку

```bash
bash mac-linux/1-install.sh     # окружение и библиотеки
bash mac-linux/2-settings.sh    # откроется .env
bash mac-linux/3-login.sh       # вход в MAX, придёт SMS
bash mac-linux/4-run.sh         # запуск
```

Что писать в `.env` — разобрано в [«Как поставить себе»](../README.md#как-поставить-себе),
раскрывашка «Шаг 2», подзаголовок «Настройки».

Пока окно терминала открыто — мост работает. Если он упадёт, скрипт поднимет его
через 10 секунд. Выключить совсем — `Ctrl+C`.

Скрипты запускаются через `bash имя-файла`, поэтому права на выполнение ставить
не нужно — это важно, если ты скачивал ZIP, а не клонировал через git: ZIP такие
права теряет.

## Автозапуск на macOS (по желанию)

В комплекте есть `deploy/ru.max-tg-bridge.plist`. Открой его, замени `ИМЯ` на
своё имя пользователя (посмотреть — команда `whoami`), потом:

```bash
cp deploy/ru.max-tg-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/ru.max-tg-bridge.plist
```

Теперь мост поднимается при входе в систему и перезапускается сам. Логи —
в `/tmp/max-tg-bridge.log`. Выключить: `launchctl unload ~/Library/LaunchAgents/ru.max-tg-bridge.plist`.

Для Linux с постоянно включённой машиной лучше подойдёт служба systemd — она
описана в [«Установке на VPS»](vps.md#сделай-службу).

---

Что-то пошло не так → [«Что могло пойти не так»](troubleshooting.md#mac-и-linux).
