"""Разовый вход в MAX по SMS. Запусти один раз, дальше сессия берётся из файла."""

import asyncio

from pymax import Client

from bridge.config import SESSION_NAME, WORK_DIR, require


async def main() -> None:
    client = Client(
        phone=require("MAX_PHONE"),
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
