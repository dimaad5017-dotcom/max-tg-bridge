"""Разовый вход в MAX по SMS. Запусти один раз, дальше сессия берётся из файла."""

import asyncio
import os
from contextlib import suppress

from pymax import Client, ExtraConfig, RegistrationConfig
from pymax.types.domain.auth import ConfirmRegistrationResponse

from bridge.config import ENV_FILE, SESSION_NAME, WORK_DIR, normalize_phone

# После регистрации MAX отвечает tokenType=LOGIN, которого нет в pymax.AuthType, и
# pymax роняет уже выданный токен на валидации. Поле нигде не читается — ослабляем тип.
ConfirmRegistrationResponse.model_fields["token_type"].annotation = str
ConfirmRegistrationResponse.model_rebuild(force=True)


def _remember(key: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _get_phone() -> str:
    stored = os.getenv("MAX_PHONE", "").strip()
    phone = normalize_phone(stored or input("Твой номер в MAX, например +79991234567: "))
    if phone != stored:
        _remember("MAX_PHONE", phone)
        print(f"Номер сохранён как {phone} — MAX принимает только такой формат.\n")
    return phone


def _get_registration() -> RegistrationConfig:
    """Пригодится, только если аккаунта на номере ещё нет: MAX заводит его сразу после SMS."""
    stored = os.getenv("MAX_NAME", "").strip()
    if not stored:
        print("Если аккаунта MAX на этом номере ещё нет, его создадут с этим именем.")
        stored = input("Имя и фамилия (фамилия не обязательна): ").strip()
        _remember("MAX_NAME", stored)
        print()
    first_name, _, last_name = stored.partition(" ")
    return RegistrationConfig(first_name=first_name, last_name=last_name.strip() or None)


async def main() -> None:
    client = Client(
        phone=_get_phone(),
        work_dir=str(WORK_DIR),
        session_name=SESSION_NAME,
        extra_config=ExtraConfig(registration_config=_get_registration()),
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

    # stop() из on_start обрывает ожидание внутри start() — для разового входа это и есть финиш.
    with suppress(asyncio.CancelledError):
        await client.start()


if __name__ == "__main__":
    asyncio.run(main())
