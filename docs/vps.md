# Установка на VPS

Так мост работает круглосуточно и не зависит от домашнего компьютера. Инструкция
для Ubuntu или Debian. Российский VPS брать удобнее: и MAX, и Telegram с него
доступны без плясок.

Перед этим нужно пройти шаг 1 — бот и группа в Telegram. Он в
[«Как поставить себе»](../README.md#как-поставить-себе), раскрывашка «Шаг 1».

## Подготовь сервер

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
```

## Скачай и поставь

```bash
sudo mkdir -p /opt/max-tg-bridge
sudo chown "$USER" /opt/max-tg-bridge
git clone https://github.com/dimaad5017-dotcom/max-tg-bridge.git /opt/max-tg-bridge
cd /opt/max-tg-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env      # сохранить Ctrl+O, выйти Ctrl+X
```

Что писать в `.env` — разобрано в [«Как поставить себе»](../README.md#как-поставить-себе),
раскрывашка «Шаг 2», подзаголовок «Настройки».

## Войди в MAX

```bash
.venv/bin/python -m bridge.login
```

SMS придёт на твой телефон, код вводится прямо здесь. Делается один раз.

## Закрой сессию от чужих глаз

```bash
chmod 600 cache/max.db .env
```

На своём компьютере это не так важно, а на сервере — важно: там могут быть
другие пользователи. `cache/max.db` — это ключ от твоего аккаунта MAX, кто
скопирует файл, тот войдёт без SMS. В `.env` лежит токен бота. После `chown`
на пользователя `bridge` (ниже) права стоит проверить ещё раз.

## Проверь вручную

```bash
.venv/bin/python -m bridge.main
```

Убедись, что в группе появились темы и сообщения ходят. Останови — `Ctrl+C`.

## Сделай службу

В комплекте лежит готовый файл `deploy/max-tg-bridge.service`. Он рассчитан на
пользователя `bridge` и папку `/opt/max-tg-bridge` — если у тебя иначе, поправь
строки `User=` и `WorkingDirectory=`.

```bash
sudo useradd -r -s /usr/sbin/nologin bridge
sudo chown -R bridge:bridge /opt/max-tg-bridge
sudo cp deploy/max-tg-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now max-tg-bridge
```

Проверить и посмотреть логи:

```bash
systemctl status max-tg-bridge
journalctl -u max-tg-bridge -f
```

Служба перезапускается сама при падении и поднимается после перезагрузки
сервера.

## Обновиться до новой версии

```bash
cd /opt/max-tg-bridge
sudo systemctl stop max-tg-bridge
sudo -u bridge git pull
sudo -u bridge .venv/bin/pip install -r requirements.txt
sudo systemctl start max-tg-bridge
```

---

Что-то пошло не так → [«Что могло пойти не так»](troubleshooting.md).
