"""Разовый вход в MAX по SMS. Запусти один раз, дальше сессия берётся из файла."""

import asyncio
import os

from pymax import Client

from bridge.config import ENV_FILE, SESSION_NAME, WORK_DIR, normalize_phone


def _remember_phone(phone: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("MAX_PHONE="):
            lines[index] = f"MAX_PHONE={phone}"
            break
    else:
        lines.append(f"MAX_PHONE={phone}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _get_phone() -> str:
    stored = os.getenv("MAX_PHONE", "").strip()
    phone = normalize_phone(stored or input("Твой номер в MAX, например +79991234567: "))
    if phone != stored:
        _remember_phone(phone)
        print(f"Номер сохранён как {phone} — MAX принимает только такой формат.\n")
    return phone


async def main() -> None:
    client = Client(
        phone=_get_phone(),
        work_dir=str(WORK_DIR),
        session_name=SESSION_NAME,
    )

    @client.on_start()
    async def on_start(client: Client) -> None:
        me = client.me
        print(f"\nВошли. Твой id в MAX: {me.contact.id if me else 'неизвестен'}")
        print(f"Сессия сохранена: {WORK_DIR / SESSION_NAME}")

        print("\nЧаты, которые видит аккаунт:")
        for chat in client.chats or []:
            print(f"  {chat.id:>20}  {chat.title or '(личный диалог)'}")

        await client.stop()

    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
