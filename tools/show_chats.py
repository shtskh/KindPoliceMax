"""
Показывает идентификаторы чатов и каналов, где побывал бот.

Зачем: команда /chatid работает только когда бот запущен И уже добавлен
в чат. На этапе настройки обычно не выполнено ни одно из условий. При
этом события MAX копятся на сервере независимо от того, работает бот
или нет, — этот скрипт их читает и показывает id.

Запуск (бота запускать НЕ нужно):

    python -m tools.show_chats

Токен берётся из .env или переменной окружения MAX_BOT_TOKEN.

ВАЖНО, ПРОВЕРЕНО НА ЖИВОМ API:
Запрос GET /updates без параметра marker сдвигает серверный указатель —
прочитанные события повторно уже не придут. Поэтому:

  * скрипт сначала узнаёт текущий маркер, а потом перечитывает окно
    назад (marker = текущий - LOOKBACK), чтобы показать историю;
  * запускать его во время работы бота не стоит: событие достанется
    тому, кто прочитал первым, и бот может пропустить команду.
Останавливайте бота на время настройки.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from config import settings  # noqa: E402
from maxapi import MAX_API_BASE  # noqa: E402

# На сколько событий назад отматывать историю. Пробуем несколько
# глубин: слишком старый маркер MAX не принимает и возвращает пусто
# (проверено — marker на 300 назад даёт пустой ответ, на 30 работает).
LOOKBACKS = (10, 30, 60, 120)

CHAT_TYPE_LABELS = {
    "dialog": "личная переписка",
    "chat": "групповой чат",
    "channel": "канал",
}


def _fetch(headers: dict, marker: int | None) -> dict:
    params: dict = {"limit": 100, "timeout": 0}
    if marker is not None:
        params["marker"] = marker
    response = requests.get(
        f"{MAX_API_BASE}/updates", headers=headers, params=params, timeout=30
    )
    return response.json()


def main() -> int:
    token = settings.max_bot_token
    if not token:
        print("MAX_BOT_TOKEN не задан. Заполните .env или переменную окружения.")
        return 1

    headers = {"Authorization": token}

    try:
        me = requests.get(f"{MAX_API_BASE}/me", headers=headers, timeout=20).json()
    except Exception as exc:
        print(f"Не удалось связаться с MAX: {exc}")
        return 1

    if me.get("code"):
        print(f"Токен не принят: {me.get('message')}")
        return 1

    print(f"Бот: {me.get('name')} (@{me.get('username')}), id={me.get('user_id')}\n")

    try:
        probe = _fetch(headers, None)
        marker = probe.get("marker")
        updates = list(probe.get("updates") or [])

        # Отматываем историю назад: без этого скрипт показал бы только
        # то, что пришло между двумя запусками.
        if isinstance(marker, int):
            seen: set[str] = set()
            for lookback in LOOKBACKS:
                older = _fetch(headers, max(0, marker - lookback))
                for update in older.get("updates") or []:
                    # Своего id у события нет, поэтому отпечаток
                    # собираем из типа, времени и чата.
                    fingerprint = (
                        f"{update.get('update_type')}|{update.get('timestamp')}|"
                        f"{(update.get('message') or {}).get('timestamp')}"
                    )
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        updates.append(update)
    except Exception as exc:
        print(f"Не удалось получить события: {exc}")
        return 1

    if not updates:
        print("Событий нет.\n\n"
              "Что сделать:\n"
              "  1. Добавьте бота в группу модераторов и в канал.\n"
              "  2. Напишите там любое сообщение.\n"
              "  3. Запустите этот скрипт снова.")
        return 0

    found: dict[int, dict] = {}
    people: dict[int, str] = {}

    for update in updates:
        message = update.get("message") or {}
        recipient = message.get("recipient") or {}
        sender = message.get("sender") or update.get("user") or {}

        if sender.get("user_id"):
            people[sender["user_id"]] = sender.get("name") or "без имени"

        chat_id = recipient.get("chat_id") or update.get("chat_id")
        if chat_id is None:
            continue

        chat_type = recipient.get("chat_type") or (
            "channel" if update.get("is_channel") else ""
        )
        entry = found.setdefault(chat_id, {"type": chat_type, "events": set()})
        entry["events"].add(update.get("update_type", "?"))
        if chat_type and not entry["type"]:
            entry["type"] = chat_type

    if found:
        print("Найденные чаты:\n")
        for chat_id, info in found.items():
            label = CHAT_TYPE_LABELS.get(info["type"], info["type"] or "неизвестно")
            print(f"  chat_id = {chat_id}   ({label})")
            print(f"      события: {', '.join(sorted(info['events']))}")

        print("\nЧто куда вписать:")
        print("  группа модераторов -> MAX_MODERATOR_CHAT_ID")
        print("  канал публикации   -> MAX_PUBLISH_CHAT_ID")

    if people:
        print("\nПользователи из событий (для MAX_ADMIN_IDS):")
        for user_id, name in people.items():
            print(f"  {user_id} — {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
