"""
Проверка сценариев MAX-бота без обращения к сети.

Вместо запуска длинного опроса подменяем клиент MAX записывающей
заглушкой и скармливаем боту самодельные события — такие же, какие
приходят при нажатии кнопки или отправке сообщения.

Так проверяется то, что юнит-тестами не поймать: доходит ли человек до
каждого раздела, не видит ли обычный пользователь служебных команд,
укладываются ли тексты в лимит MAX, валидна ли разметка.

Запуск:  python -m tests.test_max_flow
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP_DB = os.path.join(tempfile.gettempdir(), "kindpolice_max_flow.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.unlink(_TMP_DB + suffix)
    except OSError:
        pass

os.environ["DB_PATH"] = _TMP_DB
os.environ["MAX_BOT_TOKEN"] = "test-token"
os.environ["MAX_MODERATOR_CHAT_ID"] = "999"
os.environ["MAX_PUBLISH_CHAT_ID"] = "888"
os.environ["LOG_TO_FILE"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"
# Помощник не должен ходить в сеть: проверяем маршрутизацию, а не модель.
os.environ["AI_PROVIDER_API_KEY"] = ""

import max_bot  # noqa: E402
from config import settings  # noqa: E402
from content.help_content import SITUATIONS  # noqa: E402
from content.resources_seed import build_seed_resources  # noqa: E402
from maxapi import MESSAGE_LIMIT, parse_update  # noqa: E402
from storage import db  # noqa: E402
from storage.models import BotUser, UserRole  # noqa: E402

_PASSED = 0
_FAILED = 0

USER_ID = 4242
CHAT_ID = 4242


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  ok   {name}")
    else:
        _FAILED += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


class RecordingBot:
    """Заглушка клиента MAX: ничего не шлёт, всё записывает."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.answers: list[dict] = []

    def send_message(self, *, chat_id=None, user_id=None, text="",
                     buttons=None, image_path=None, notify=True):
        self.sent.append({
            "chat_id": chat_id, "user_id": user_id, "text": text,
            "buttons": buttons or [], "image_path": image_path,
        })
        return {"message": {"body": {"mid": "mid.test"}}}

    def answer_callback(self, callback_id, *, notification=None,
                        text=None, buttons=None):
        self.answers.append({"callback_id": callback_id,
                             "notification": notification})
        return True

    def clear(self) -> None:
        self.sent.clear()
        self.answers.clear()

    # -- выборки ------------------------------------------------------------

    @property
    def all_text(self) -> str:
        return "\n".join(m["text"] for m in self.sent)

    def payloads(self) -> list[str]:
        out = []
        for message in self.sent:
            for row in message["buttons"]:
                for button in row:
                    if button.get("payload"):
                        out.append(button["payload"])
        return out

    def button_labels(self) -> list[str]:
        return [b.get("text", "") for m in self.sent
                for row in m["buttons"] for b in row]


recorder = RecordingBot()
max_bot.bot = recorder


def send_text(text: str, *, user_id: int = USER_ID, chat_type: str = "dialog"):
    recorder.clear()
    max_bot.handle_update(parse_update({
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": user_id, "name": "Тест", "username": "tester"},
            "recipient": {"chat_id": CHAT_ID, "chat_type": chat_type},
            "body": {"mid": "mid.1", "text": text},
        },
    }))
    return recorder


def press(payload: str, *, user_id: int = USER_ID):
    recorder.clear()
    max_bot.handle_update(parse_update({
        "update_type": "message_callback",
        "callback": {"callback_id": "cb", "payload": payload,
                     "user": {"user_id": user_id, "name": "Тест"}},
        "message": {"recipient": {"chat_id": CHAT_ID, "chat_type": "dialog"},
                    "body": {"mid": "mid.2", "text": ""}},
    }))
    return recorder


# --------------------------------------------------------------------------

_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9-]+)[^>]*>")


def html_is_valid(text: str) -> tuple[bool, str]:
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        tag = match.group(1).lower()
        if tag not in _ALLOWED_TAGS:
            return False, f"недопустимый тег <{tag}>"
        if match.group(0).startswith("</"):
            if not stack or stack[-1] != tag:
                return False, f"непарный </{tag}>"
            stack.pop()
        else:
            stack.append(tag)
    return (not stack), (f"незакрытый <{stack[-1]}>" if stack else "")


def check_outgoing(label: str) -> None:
    for message in recorder.sent:
        text = message["text"]
        if len(text) > MESSAGE_LIMIT:
            check(f"{label}: лимит MAX", False, f"{len(text)} > {MESSAGE_LIMIT}")
            return
        ok, reason = html_is_valid(text)
        if not ok:
            check(f"{label}: валидный HTML", False, f"{reason} в «{text[:70]}…»")
            return
        for row in message["buttons"]:
            for button in row:
                if button.get("type") == "callback" and len(button["payload"]) > 64:
                    check(f"{label}: payload кнопки", False, button["payload"])
                    return
    check(f"{label}: разметка и лимиты в порядке", True)


# --------------------------------------------------------------------------
# Сценарии
# --------------------------------------------------------------------------

def scenario_public() -> None:
    print("\nОбычный пользователь")

    send_text("/start")
    check("бот ответил на /start", bool(recorder.sent))
    check("показано меню", "m:sit" in recorder.payloads())
    check("служебной справки НЕ видно", "/fetch" not in recorder.all_text)
    check_outgoing("/start")

    stats = db.count_audience(settings.db_path)
    check("пользователь учтён", stats["total"] >= 1)

    press("m:ph")
    check("телефоны: есть 112", "112" in recorder.all_text)
    check("телефоны: есть телефон доверия", "222-74-47" in recorder.all_text)
    check_outgoing("телефоны")

    press("m:sit")
    check("список ситуаций открылся", "Что делать" in recorder.all_text)
    check_outgoing("ситуации")

    for situation in SITUATIONS:
        press(f"s:{situation.key}")
        check(f"памятка: {situation.button}", situation.title in recorder.all_text)
        check_outgoing(f"памятка {situation.key}")

    press("m:res")
    check("справочник открылся", "Официальные источники" in recorder.all_text)
    check_outgoing("справочник")

    for key in ("max", "tg", "edu"):
        press(f"r:{key}:0")
        check(f"категория {key}", bool(recorder.sent))
        check_outgoing(f"категория {key}")

    press("r:max:5")
    check("пагинация регионов", bool(recorder.sent))
    press("rr:max:0")
    check("регион открылся", bool(recorder.sent))
    press("rr:max:9999")
    check("некорректный индекс не роняет", True)

    press("m:reg")
    check("выбор региона", "регион" in recorder.all_text.lower())
    press("g:0")
    check("регион сохранён",
          db.get_audience_region(settings.db_path, USER_ID) is not None)
    check_outgoing("карточка региона")
    press("gc")
    check("регион сброшен",
          db.get_audience_region(settings.db_path, USER_ID) is None)

    press("m:news")
    check("новости отвечают", bool(recorder.sent))
    press("m:edu")
    check("учёба отвечает", "вуз" in recorder.all_text.lower())
    press("m:about")
    check("о проекте отвечает", "проект" in recorder.all_text.lower())
    check_outgoing("о проекте")


def scenario_assistant_offline() -> None:
    print("\nПомощник без ключа AI")

    press("m:ask")
    check("помощник отвечает без ключа", bool(recorder.sent))
    check("предлагает памятки",
          "памятк" in recorder.all_text.lower() or "Что делать" in recorder.all_text)

    send_text("У меня украли телефон, что делать?")
    check("свободный текст обработан", bool(recorder.sent))
    check_outgoing("свободный текст")

    for greeting in ("привет", "Спасибо!"):
        send_text(greeting)
        check(f"«{greeting}» не уходит модели",
              "Здравствуйте" in recorder.all_text or "Выберите раздел" in recorder.all_text)


def scenario_access() -> None:
    print("\nРазграничение доступа")

    for command in ("/fetch", "/diag", "/stats", "/audience", "/broadcast текст"):
        send_text(command)
        check(f"{command} закрыт для обычного пользователя",
              "нет доступа" in recorder.all_text.lower(),
              recorder.all_text[:60])

    # Кнопки модерации тоже недоступны: нажатие от постороннего
    # не должно ничего публиковать.
    press("a:00000000-0000-0000-0000-000000000000")
    check("кнопка «Опубликовать» закрыта для постороннего",
          any(a["notification"] == "Нет доступа" for a in recorder.answers))
    check("ничего не опубликовано", not recorder.sent)


def scenario_staff() -> None:
    print("\nМодератор и администратор")

    db.upsert_user(settings.db_path,
                   BotUser(telegram_id=USER_ID, role=UserRole.ADMIN, username="admin"))

    send_text("/start")
    check("сотрудник видит служебную справку", "/fetch" in recorder.all_text)
    check_outgoing("/start для админа")

    for command, expected in (
        ("/help", "/fetch"),
        ("/stats", "Статистика"),
        ("/audience", "Аудитория"),
        ("/diag", "Диагностика"),
        ("/schedule_status", "Автосбор"),
        ("/whoami", "Ваш ID"),
        ("/chatid", "ID этого чата"),
        ("/list_moderators", "Доступ к боту"),
        ("/pending", "Очередь пуста"),
        ("/list_resources institute", "edu_"),
        ("/add_moderator", "Использование"),
        ("/broadcast", "Использование"),
        ("/clear", "Очередь уже пуста"),
    ):
        send_text(command)
        check(f"{command}", expected in recorder.all_text,
              recorder.all_text[:80])
        check_outgoing(command)

    send_text("/add_moderator 777")
    check("модератор назначен",
          db.get_user(settings.db_path, 777) is not None)
    send_text("/remove_moderator 777")
    check("модератор снят", db.get_user(settings.db_path, 777) is None)

    send_text("/несуществующая")
    check("неизвестная команда не роняет бота", bool(recorder.sent))


def scenario_moderation() -> None:
    print("\nМодерация новости")

    from datetime import datetime, timezone
    from storage.models import NewsItem, NewsStatus

    item = NewsItem(
        id="11111111-1111-1111-1111-111111111111",
        source_name="Telegram: @mediamvd",
        source_url="https://t.me/mediamvd/1",
        title="Заголовок",
        raw_text="Полицейские спасли ребёнка из горящего дома в Казани утром.",
        published_at=datetime.now(timezone.utc),
        status=NewsStatus.PENDING_MODERATION,
        region="Республика Татарстан", city="Казань",
        rewritten_text="Полицейские <b>спасли ребёнка</b> в <i>Казани</i>.",
    )
    db.save_item(settings.db_path, item)

    recorder.clear()
    max_bot.send_to_moderators(item)
    check("карточка ушла в чат модераторов",
          recorder.sent and recorder.sent[0]["chat_id"] == 999,
          str(recorder.sent[:1])[:120])
    check("на карточке три кнопки", len(recorder.payloads()) == 3)
    check_outgoing("карточка модерации")

    press(f"a:{item.id}")
    published = [m for m in recorder.sent if m["chat_id"] == 888]
    check("пост ушёл в канал публикации", bool(published))
    check("в канал ушёл текст без служебной справки",
          published and "🖼" not in published[0]["text"])
    check("статус стал published",
          db.get_by_id(settings.db_path, item.id).status.value == "published")

    # Повторное нажатие не должно публиковать второй раз.
    recorder.clear()
    press(f"a:{item.id}")
    check("повторная публикация не произошла",
          not [m for m in recorder.sent if m["chat_id"] == 888])


def main() -> int:
    db.init_db(settings.db_path)
    db.add_resources_bulk(settings.db_path, build_seed_resources())

    for scenario in (scenario_public, scenario_assistant_offline,
                     scenario_access, scenario_staff, scenario_moderation):
        try:
            scenario()
        except Exception:
            global _FAILED
            _FAILED += 1
            print(f"\n  ИСКЛЮЧЕНИЕ в {scenario.__name__}:")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Пройдено: {_PASSED}   Провалено: {_FAILED}")
    print("=" * 60)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
