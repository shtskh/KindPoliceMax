"""
Бот «Хороший полицейский» для мессенджера MAX.

Это самостоятельный бот: он сам собирает новости, сам их модерирует и
сам публикует в канал MAX. С Telegram-версией он не связан — у каждой
свои токены, своя база и свой сервер.

ЧЕМ ОТЛИЧАЕТСЯ ОТ TELEGRAM-ВЕРСИИ (из-за возможностей API MAX)

  * Нет постоянной клавиатуры снизу экрана. В MAX кнопки бывают только
    inline — прикреплёнными к сообщению. Поэтому главное меню здесь не
    «всегда на экране», а сообщение с кнопками, которое открывается по
    /start, /menu и кнопкой «Меню» в конце каждого раздела.
  * Нет состояний FSM из aiogram, поэтому ожидание вопроса к помощнику
    хранится в простом словаре в памяти процесса. Потеря этого
    состояния при перезапуске некритична: человек просто нажмёт кнопку
    «Задать вопрос» заново.
  * Нет готового диспетчера — события разбираются вручную в одном
    цикле длинного опроса (poll_forever).

РАЗГРАНИЧЕНИЕ ДОСТУПА

Служебные функции (сбор, модерация, статистика, рассылка) доступны
только пользователям с ролью в таблице bot_users, а карточки на
модерацию уходят в закрытый чат MAX_MODERATOR_CHAT_ID. Обычный
пользователь не видит ни служебных команд, ни карточек модерации:
на такие команды бот ему отвечает отказом.
"""
from __future__ import annotations

import html
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from config import settings, validate_settings
from content.help_content import (
    ABOUT_TEXT, DISCLAIMER, EDUCATION_INFO, EMERGENCY_PHONES,
    SITUATION_BY_KEY, SITUATIONS, TRUST_PHONES,
)
from content.resources_seed import build_seed_resources, seed_counts
from logging_setup import setup_logging
from maxapi import MaxBot, MaxUpdate, callback_button, link_button
from paths import resolve
from pipeline import assistant, image_pipeline
from pipeline.orchestrator import regenerate_image, run_collection_cycle
from pipeline.post_formatting import FOOTER_MARKER
from storage import db
from storage.models import (
    BotUser, NewsStatus, PublicResource, ResourceCategory, UserRole,
)

logger = setup_logging(settings.log_level, settings.log_to_file)

bot: MaxBot | None = None

# Пользователи, от которых ждём текст вопроса к ИИ-помощнику.
_awaiting_question: set[int] = set()

# Сбор новостей идёт в отдельном потоке, чтобы не вешать опрос событий.
_worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="work")
_collection_lock = threading.Lock()


# ==========================================================================
# Доступ
# ==========================================================================

_ROLE_ORDER = {UserRole.MODERATOR: 1, UserRole.ADMIN: 2}


def _role_allows(role: UserRole, minimum: UserRole) -> bool:
    return _ROLE_ORDER[role] >= _ROLE_ORDER[minimum]


def has_role(user_id: int | None, minimum: UserRole) -> bool:
    if user_id is None:
        return False
    user = db.get_user(settings.db_path, user_id)
    return user is not None and _role_allows(user.role, minimum)


def bootstrap_admins() -> None:
    """Выдаёт роль admin всем, кто указан в MAX_ADMIN_IDS."""
    for admin_id in settings.bot_admin_ids:
        existing = db.get_user(settings.db_path, admin_id)
        if existing is None or existing.role != UserRole.ADMIN:
            db.upsert_user(
                settings.db_path,
                BotUser(
                    telegram_id=admin_id,  # в MAX сюда пишется MAX user_id
                    role=UserRole.ADMIN,
                    username=existing.username if existing else None,
                    full_name=existing.full_name if existing else None,
                ),
            )
            logger.info("Администратор из MAX_ADMIN_IDS: %s", admin_id)


def seed_public_resources(update_existing: bool = False) -> int:
    added = db.add_resources_bulk(
        settings.db_path, build_seed_resources(), update_existing=update_existing
    )
    if added:
        logger.info("В публичное меню добавлено источников: %d", added)
    return added


# ==========================================================================
# Отправка
# ==========================================================================

def reply(update: MaxUpdate, text: str, buttons=None, image_path=None) -> None:
    """
    Отвечает туда же, откуда пришло событие.

    В личной переписке у события может не быть chat_id — тогда отвечаем
    по user_id, иначе сообщение просто некуда адресовать.
    """
    if update.chat_id is not None:
        bot.send_message(chat_id=update.chat_id, text=text,
                         buttons=buttons, image_path=image_path)
    elif update.user_id is not None:
        bot.send_message(user_id=update.user_id, text=text,
                         buttons=buttons, image_path=image_path)


def _track(update: MaxUpdate) -> None:
    """Учитывает обычного пользователя. Сбой учёта не должен мешать ответу."""
    if update.user_id is None:
        return
    try:
        db.touch_audience_user(
            settings.db_path,
            telegram_id=update.user_id,
            username=update.username,
            full_name=update.user_name,
        )
    except Exception:
        logger.debug("Не удалось учесть пользователя", exc_info=True)


# ==========================================================================
# Оформление
# ==========================================================================

_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_IMAGE_SOURCE_LABELS = {
    "original": "фото из источника",
    "generated": "сгенерировано ИИ",
    "card": "фирменная карточка",
    "none": "без изображения",
}


def _format_date_ru(value: datetime | None) -> str:
    if not value:
        return ""
    try:
        return f"{value.day} {_MONTHS_RU[value.month - 1]} {value.year}"
    except (IndexError, ValueError, AttributeError):
        return ""


def clean_text(text: str) -> str:
    """Убирает мусорные строки, переживавшие рерайт."""
    if not text:
        return ""

    bad_substrings = ("подписывайтесь", "подписаться", "изображение: original")
    bad_words = {"telegram:", "max:", "vk:", "vk.com", "t.me"}

    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(fragment in lower for fragment in bad_substrings):
            continue
        if bad_words & set(lower.replace(",", " ").split()):
            continue
        if lower.startswith(("http://", "https://", "www.")):
            continue
        lines.append(line)

    return " ".join(" ".join(lines).split())


def build_post_text(item) -> str:
    """Текст, который уходит в канал MAX."""
    if not item.rewritten_text:
        return html.escape(clean_text(item.raw_text or item.title or ""))

    body_part, marker, footer_part = item.rewritten_text.partition(FOOTER_MARKER)
    body = clean_text(body_part)
    parts = [body] if body else []
    if footer_part:
        parts.append(marker + footer_part)
    return "\n\n".join(parts).strip()


def build_moderation_text(item) -> str:
    """Карточка для чата модераторов: пост плюс служебная справка."""
    lines = []

    place = " · ".join(p for p in (item.city, item.region) if p)
    header = []
    if place:
        header.append(f"📍 <b>{html.escape(place)}</b>")
    date_label = _format_date_ru(item.published_at)
    if date_label:
        header.append(f"🗓 {date_label}")
    if header:
        lines.append("   ".join(header))

    lines.append(build_post_text(item) or "<i>Текст новости пуст.</i>")

    footer = ["➖➖➖➖➖",
              f"🖼 {_IMAGE_SOURCE_LABELS.get(item.image_source, item.image_source or 'нет')}",
              f"🔗 {html.escape(item.source_name or 'источник неизвестен')}"]
    if item.tags:
        footer.append("🏷 " + ", ".join(html.escape(t) for t in item.tags))
    if item.verification_notes:
        icon = "✅" if "доверенный" in item.verification_notes.lower() else "⚠️"
        footer.append(f"{icon} <i>{html.escape(item.verification_notes)}</i>")

    lines.append("\n".join(footer))
    return "\n\n".join(lines).strip()


# ==========================================================================
# Главное меню
# ==========================================================================

MENU_BUTTONS = [
    [callback_button("🆘 Что делать, если…", "m:sit"),
     callback_button("☎️ Телефоны", "m:ph")],
    [callback_button("💬 Задать вопрос", "m:ask"),
     callback_button("🏛 Источники МВД", "m:res")],
    [callback_button("📰 Хорошие новости", "m:news"),
     callback_button("🎓 Учёба в МВД", "m:edu")],
    [callback_button("📍 Мой регион", "m:reg"),
     callback_button("ℹ️ О проекте", "m:about")],
]

BACK_TO_MENU = [callback_button("⬅️ Меню", "m:home")]

GREETING = (
    "👮 <b>Хороший полицейский</b>\n\n"
    "Здесь собраны истории о том, как сотрудники полиции спасают, "
    "помогают и поддерживают людей.\n\n"
    "<b>А ещё бот поможет разобраться:</b>\n"
    "🆘 памятки «что делать, если…» — кража, ДТП, мошенники, пропал человек\n"
    "☎️ телефоны экстренных служб\n"
    "💬 вопрос своими словами — подскажу, куда идти и какие у вас права\n"
    "🏛 официальные каналы МВД по всем регионам\n"
    "🎓 как поступить в вуз МВД\n\n"
    "Выберите раздел:"
)

PUBLIC_HELP = (
    "👮 <b>Что умеет бот</b>\n\n"
    "/menu — главное меню\n"
    "/situations — что делать, если…\n"
    "/phones — экстренные телефоны\n"
    "/ask — задать вопрос помощнику\n"
    "/resources — официальные источники МВД\n"
    "/news — хорошие новости\n"
    "/education — учёба в МВД\n"
    "/region — выбрать свой регион\n"
    "/about — о проекте\n"
    "/whoami — ваш идентификатор и роль\n\n"
    "Можно просто написать вопрос обычными словами.\n\n"
    "🚨 Если случилось происшествие — звоните <b>102</b> или <b>112</b>."
)


def staff_help(role: UserRole) -> str:
    lines = [
        "🛠 <b>Служебные команды</b>\n",
        "/fetch — собрать и обработать свежие новости",
        "/pending — показать очередь модерации",
        "/stats — статистика работы бота",
        "/audience — статистика по пользователям",
        "/schedule_status — статус автосбора",
        "/chatid — идентификатор текущего чата",
    ]
    if role == UserRole.ADMIN:
        lines += [
            "",
            "<b>Администрирование</b>",
            "/diag — диагностика настроек",
            "/schedule_on, /schedule_off, /set_interval N",
            "/add_moderator ID, /remove_moderator ID, /list_moderators",
            "/clear → /clear_confirm — архивировать очередь",
            "/cleanup_images — удалить неиспользуемые картинки",
            "/assistant_log — последние вопросы к помощнику",
            "/broadcast текст — рассылка пользователям",
            "/add_resource, /list_resources, /remove_resource, /reseed",
            "/test_publish — тестовый пост в канал",
        ]
    return "\n".join(lines)


# ==========================================================================
# Разделы для обычных пользователей
# ==========================================================================

def show_menu(update: MaxUpdate) -> None:
    reply(update, GREETING, MENU_BUTTONS)


def show_phones(update: MaxUpdate) -> None:
    lines = ["☎️ <b>Экстренные службы</b>\n"]
    for phone in EMERGENCY_PHONES:
        lines.append(f"<b>{phone.number}</b> — {phone.title}")
        if phone.note:
            lines.append(f"    <i>{phone.note}</i>")

    lines.append("\n📞 <b>Телефоны доверия</b>\n")
    for phone in TRUST_PHONES:
        lines.append(f"<b>{phone.number}</b> — {phone.title}")
        if phone.note:
            lines.append(f"    <i>{phone.note}</i>")

    lines.append(
        "\n<i>Звонок на 112 проходит без денег на счёте, без SIM-карты "
        "и при заблокированном экране.</i>"
    )
    reply(update, "\n".join(lines),
          [[callback_button("🆘 Что делать, если…", "m:sit")], BACK_TO_MENU])


def situations_buttons() -> list[list[dict]]:
    rows = [[callback_button(s.button, f"s:{s.key}")] for s in SITUATIONS]
    rows.append([callback_button("☎️ Экстренные телефоны", "m:ph")])
    rows.append(BACK_TO_MENU)
    return rows


def show_situations(update: MaxUpdate) -> None:
    reply(update,
          "🆘 <b>Что делать, если…</b>\n\n"
          "Короткие памятки по типовым ситуациям — что делать по шагам "
          "и куда обращаться.\n\nВыберите ситуацию:",
          situations_buttons())


def show_situation(update: MaxUpdate, key: str) -> None:
    situation = SITUATION_BY_KEY.get(key)
    if situation is None:
        reply(update, "Раздел не найден.", [BACK_TO_MENU])
        return

    lines = [f"<b>{situation.title}</b>\n"]
    for index, step in enumerate(situation.steps, 1):
        lines.append(f"<b>{index}.</b> {step}")
    if situation.warning:
        lines.append(f"\n⚠️ <b>Важно.</b> {situation.warning}")
    if situation.phones:
        lines.append("\n☎️ " + " · ".join(f"<b>{p}</b>" for p in situation.phones))
    lines.append(f"\n{DISCLAIMER}")

    reply(update, "\n".join(lines), [
        [callback_button("💬 Задать свой вопрос", "m:ask")],
        [callback_button("⬅️ Все ситуации", "m:sit")],
        BACK_TO_MENU,
    ])


ASSISTANT_INTRO = (
    "💬 <b>Задайте вопрос</b>\n\n"
    "Помогу разобраться: куда обращаться, как подать заявление, "
    "какие у вас права, что делать в конкретной ситуации.\n\n"
    "Напишите вопрос обычными словами — например:\n"
    "<i>«У меня украли телефон, что делать?»</i>\n\n"
    "⚠️ Это <b>справочная информация, а не юридическая консультация</b>.\n"
    "🚨 Если опасность прямо сейчас — звоните <b>112</b> или <b>102</b>."
)


def start_ask(update: MaxUpdate) -> None:
    if not settings.assistant_enabled or not settings.ai_configured:
        reply(update,
              "Помощник сейчас отключён. Загляните в раздел "
              "«Что делать, если…» — там готовые памятки.",
              situations_buttons())
        return

    if update.user_id is not None:
        _awaiting_question.add(update.user_id)
    reply(update, ASSISTANT_INTRO, [
        [callback_button("✖️ Отмена", "m:home")],
    ])


def answer_question(update: MaxUpdate, question: str) -> None:
    user_id = update.user_id
    if user_id is None:
        return

    _awaiting_question.discard(user_id)
    allowed, remaining = assistant.check_rate_limit(user_id)
    if not allowed:
        reply(update,
              "Вы задали много вопросов за сутки — лимит исчерпан. "
              "Попробуйте завтра или загляните в «Что делать, если…».",
              [BACK_TO_MENU])
        return

    reply(update, "💭 Читаю вопрос…")
    result = assistant.ask_assistant(question, user_id)

    text = result.text
    if result.ok and not result.refused and remaining <= 5:
        text += f"\n\n<i>Осталось вопросов на сегодня: {remaining - 1}</i>"

    reply(update, text, [
        [callback_button("💬 Ещё вопрос", "m:ask")],
        [callback_button("🆘 Готовые памятки", "m:sit")],
        BACK_TO_MENU,
    ])


# -- справочник источников --------------------------------------------------

_CATEGORY_KEYS = {
    "tg": ResourceCategory.MVD_TELEGRAM.value,
    "max": ResourceCategory.MVD_MAX.value,
    "edu": ResourceCategory.INSTITUTE.value,
}
_CATEGORY_LABELS = {
    ResourceCategory.MVD_MAX.value: "🅼 МВД в MAX",
    ResourceCategory.MVD_TELEGRAM.value: "📢 МВД в Telegram",
    ResourceCategory.INSTITUTE.value: "🎓 Вузы МВД России",
}
REGIONS_PER_PAGE = 8


def show_resources_home(update: MaxUpdate) -> None:
    counts = db.count_resources(settings.db_path)
    total = sum(counts.values())
    # MAX первым: в этом мессенджере человеку полезнее местный канал MAX.
    rows = [
        [callback_button(_CATEGORY_LABELS[value], f"r:{key}:0")]
        for key, value in (("max", _CATEGORY_KEYS["max"]),
                           ("tg", _CATEGORY_KEYS["tg"]),
                           ("edu", _CATEGORY_KEYS["edu"]))
    ]
    rows.append(BACK_TO_MENU)
    reply(update,
          f"🏛 <b>Официальные источники</b>\n\n"
          f"В справочнике {total} проверенных ссылок.\n\nВыберите категорию:",
          rows)


def show_resource_category(update: MaxUpdate, key: str, page: int) -> None:
    category = _CATEGORY_KEYS.get(key)
    if not category:
        reply(update, "Неизвестная категория.", [BACK_TO_MENU])
        return

    regions = db.list_resource_regions(settings.db_path, category)
    federal = db.list_resources(settings.db_path, category=category, federal_only=True)

    rows: list[list[dict]] = [[link_button(f"⭐ {r.name}", r.url)] for r in federal[:6]]

    total_pages = max(1, (len(regions) + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    for region in regions[page * REGIONS_PER_PAGE:(page + 1) * REGIONS_PER_PAGE]:
        rows.append([callback_button(f"📍 {region}",
                                     f"rr:{key}:{regions.index(region)}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(callback_button("◀️", f"r:{key}:{page - 1}"))
        nav.append(callback_button(f"{page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(callback_button("▶️", f"r:{key}:{page + 1}"))
        rows.append(nav)

    rows.append([callback_button("⬅️ Категории", "m:res")])

    caption = f"<b>{_CATEGORY_LABELS[category]}</b>\n\n"
    caption += (f"Федеральные источники — кнопками выше.\n"
                f"Ниже выберите регион ({len(regions)} доступно):"
                if regions else "Выберите источник:")
    reply(update, caption, rows)


def show_resource_region(update: MaxUpdate, key: str, index: int) -> None:
    category = _CATEGORY_KEYS.get(key)
    if not category:
        return
    regions = db.list_resource_regions(settings.db_path, category)
    if not 0 <= index < len(regions):
        reply(update, "Регион не найден — откройте меню заново.", [BACK_TO_MENU])
        return

    region = regions[index]
    items = db.list_resources(settings.db_path, category=category, region=region)
    rows = [[link_button(item.name, item.url)] for item in items[:12]]
    rows.append([callback_button("⬅️ Назад", f"r:{key}:0")])
    reply(update,
          f"{_CATEGORY_LABELS[category]}\n📍 <b>{html.escape(region)}</b>", rows)


# -- новости ---------------------------------------------------------------

def show_news(update: MaxUpdate) -> None:
    region = (db.get_audience_region(settings.db_path, update.user_id)
              if update.user_id else None)
    items = db.recent_published(settings.db_path, limit=5, region=region)
    fallback = False
    if not items and region:
        items = db.recent_published(settings.db_path, limit=5)
        fallback = True

    if not items:
        reply(update, "📰 Пока нет опубликованных новостей.", [BACK_TO_MENU])
        return

    header = "📰 <b>Последние хорошие новости</b>"
    if region and not fallback:
        header += f"\n<i>Ваш регион: {html.escape(region)}</i>"
    elif fallback:
        header += f"\n<i>По региону «{html.escape(region)}» постов пока нет</i>"
    reply(update, header)

    for item in items:
        place = " · ".join(p for p in (item.city, item.region) if p)
        text = (f"📍 <b>{html.escape(place)}</b>\n\n" if place else "") + build_post_text(item)
        image = (resolve(item.image_path)
                 if item.image_path and image_pipeline.image_exists(item.image_path)
                 else None)
        reply(update, text, image_path=image)
        time.sleep(0.3)

    reply(update, "Это всё за сегодня.", [BACK_TO_MENU])


# -- регион ----------------------------------------------------------------

def region_keyboard(page: int = 0) -> list[list[dict]]:
    regions = db.list_resource_regions(settings.db_path, ResourceCategory.MVD_MAX.value)
    total_pages = max(1, (len(regions) + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    rows = [[callback_button(f"📍 {region}", f"g:{regions.index(region)}")]
            for region in regions[page * REGIONS_PER_PAGE:(page + 1) * REGIONS_PER_PAGE]]

    nav = []
    if page > 0:
        nav.append(callback_button("◀️", f"gp:{page - 1}"))
    nav.append(callback_button(f"{page + 1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        nav.append(callback_button("▶️", f"gp:{page + 1}"))
    rows.append(nav)
    rows.append([callback_button("🗑 Сбросить регион", "gc")])
    rows.append(BACK_TO_MENU)
    return rows


def show_region_picker(update: MaxUpdate, page: int = 0) -> None:
    current = (db.get_audience_region(settings.db_path, update.user_id)
               if update.user_id else None)
    text = "📍 <b>Ваш регион</b>\n\n"
    if current:
        text += f"Сейчас выбран: <b>{html.escape(current)}</b>\n\n"
    text += ("Регион нужен, чтобы показывать каналы местного управления МВД "
             "и новости вашего края в первую очередь.\n\nВыберите регион:")
    reply(update, text, region_keyboard(page))


def set_region(update: MaxUpdate, index: int) -> None:
    regions = db.list_resource_regions(settings.db_path, ResourceCategory.MVD_MAX.value)
    if not 0 <= index < len(regions) or update.user_id is None:
        return
    region = regions[index]
    _track(update)
    db.set_audience_region(settings.db_path, update.user_id, region)

    rows: list[list[dict]] = []
    for category in (ResourceCategory.MVD_MAX.value,
                     ResourceCategory.MVD_TELEGRAM.value,
                     ResourceCategory.INSTITUTE.value):
        prefix = {"mvd_max": "🅼", "mvd_telegram": "📢", "institute": "🎓"}[category]
        for resource in db.list_resources(
            settings.db_path, category=category, region=region
        )[:4]:
            rows.append([link_button(f"{prefix} {resource.name}", resource.url)])
    rows.append([callback_button("📍 Сменить регион", "gp:0")])
    rows.append(BACK_TO_MENU)

    reply(update, f"📍 <b>{html.escape(region)}</b>\n\n"
                  f"Официальные источники вашего региона:", rows)


# ==========================================================================
# Модерация
# ==========================================================================

def moderation_buttons(item_id: str) -> list[list[dict]]:
    return [
        [callback_button("✅ Опубликовать", f"a:{item_id}"),
         callback_button("❌ Отклонить", f"x:{item_id}")],
        [callback_button("🖼 Другая картинка", f"i:{item_id}")],
    ]


def send_to_moderators(item, chat_id: int | None = None) -> None:
    """
    Карточка уходит в закрытый чат модераторов — обычные пользователи
    бота её не видят.
    """
    target = chat_id or _moderator_chat_id()
    if target is None:
        logger.warning("MAX_MODERATOR_CHAT_ID не задан — некуда слать на модерацию.")
        return

    image = (resolve(item.image_path)
             if item.image_path and image_pipeline.image_exists(item.image_path)
             else None)
    bot.send_message(chat_id=target, text=build_moderation_text(item),
                     buttons=moderation_buttons(item.id), image_path=image)


def _moderator_chat_id() -> int | None:
    try:
        return int(settings.moderator_chat_id)
    except (TypeError, ValueError):
        return None


def _publish_chat_id() -> int | None:
    try:
        return int(settings.publish_chat_id)
    except (TypeError, ValueError):
        return None


def approve_item(update: MaxUpdate, item_id: str) -> str:
    item = db.get_by_id(settings.db_path, item_id)
    if item is None:
        return "Новость не найдена."

    target = _publish_chat_id()
    if target is None:
        return "MAX_PUBLISH_CHAT_ID не задан."

    # Атомарный захват: второй модератор (или повторное нажатие) не
    # опубликует тот же пост второй раз.
    if not db.claim_for_moderation(
        settings.db_path, item_id, NewsStatus.APPROVED, update.user_id
    ):
        current = db.get_by_id(settings.db_path, item_id)
        return f"Уже обработана (статус: {current.status.value if current else '?'})."

    image = (resolve(item.image_path)
             if item.image_path and image_pipeline.image_exists(item.image_path)
             else None)
    sent = bot.send_message(chat_id=target, text=build_post_text(item),
                            image_path=image)

    if sent is None:
        # Возвращаем в очередь, иначе новость застрянет в статусе approved.
        db.update_status(settings.db_path, item_id, NewsStatus.PENDING_MODERATION)
        return "Не удалось опубликовать, новость вернулась в очередь."

    db.update_status(settings.db_path, item_id, NewsStatus.PUBLISHED)
    logger.info("Новость %s опубликована пользователем %s", item_id, update.user_id)
    return "Опубликовано"


# ==========================================================================
# Сбор новостей
# ==========================================================================

SCHEDULER_CONFIG_KEY = "scheduler"
MIN_INTERVAL_MINUTES = 10


def _get_scheduler_state() -> dict:
    return db.get_config(settings.db_path, SCHEDULER_CONFIG_KEY, default={
        "enabled": False,
        "interval_minutes": settings.default_post_interval_minutes,
        "last_run_at": None,
    })


def _set_scheduler_state(state: dict) -> None:
    db.set_config(settings.db_path, SCHEDULER_CONFIG_KEY, state)


def _mark_scheduler_run() -> None:
    state = _get_scheduler_state()
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    _set_scheduler_state(state)


def run_collection(notify_chat: int | None = None) -> None:
    """Сбор идёт в рабочем потоке, чтобы не останавливать опрос событий."""
    if not _collection_lock.acquire(blocking=False):
        if notify_chat:
            bot.send_message(chat_id=notify_chat,
                             text="⏳ Сбор уже выполняется, подождите.")
        return

    try:
        _mark_scheduler_run()
        items, stats = run_collection_cycle()
        for item in items:
            try:
                send_to_moderators(item)
            except Exception:
                logger.exception("Не удалось отправить новость %s", item.id)

        if notify_chat:
            header = ("✅ <b>Сбор завершён</b>" if items
                      else "🔍 <b>Новых подходящих новостей не найдено</b>")
            bot.send_message(chat_id=notify_chat,
                             text=header + "\n\n" + "\n".join(stats.summary_lines()))
    except Exception as exc:
        logger.exception("Ошибка сбора новостей")
        if notify_chat:
            bot.send_message(chat_id=notify_chat,
                             text=f"❌ Ошибка сбора: {html.escape(str(exc))[:300]}")
    finally:
        _collection_lock.release()


def scheduler_loop() -> None:
    """Фоновый поток: раз в минуту проверяет, не пора ли собирать."""
    while True:
        time.sleep(60)
        try:
            state = _get_scheduler_state()
            if not state.get("enabled"):
                continue

            interval = state.get("interval_minutes",
                                 settings.default_post_interval_minutes)
            last_run = state.get("last_run_at")
            due = True
            if last_run:
                try:
                    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_run)
                    due = elapsed >= timedelta(minutes=interval)
                except ValueError:
                    due = True
            if not due or _collection_lock.locked():
                continue

            logger.info("Плановый автосбор...")
            run_collection(_moderator_chat_id())
        except Exception:
            logger.exception("Ошибка планового автосбора")


# ==========================================================================
# Команды
# ==========================================================================

def handle_command(update: MaxUpdate, command: str, args: str) -> None:
    user_id = update.user_id
    is_moderator = has_role(user_id, UserRole.MODERATOR)
    is_admin = has_role(user_id, UserRole.ADMIN)

    # --- публичные ---
    if command in ("start", "menu"):
        _awaiting_question.discard(user_id or 0)
        show_menu(update)
        if is_moderator:
            reply(update, staff_help(
                UserRole.ADMIN if is_admin else UserRole.MODERATOR))
        return
    if command == "help":
        reply(update, PUBLIC_HELP, [BACK_TO_MENU])
        if is_moderator:
            reply(update, staff_help(
                UserRole.ADMIN if is_admin else UserRole.MODERATOR))
        return
    if command == "situations":
        show_situations(update); return
    if command == "phones":
        show_phones(update); return
    if command == "resources":
        show_resources_home(update); return
    if command == "news":
        show_news(update); return
    if command == "education":
        reply(update, EDUCATION_INFO,
              [[callback_button("🎓 Список вузов", "r:edu:0")], BACK_TO_MENU])
        return
    if command == "region":
        show_region_picker(update); return
    if command == "about":
        reply(update, ABOUT_TEXT, [BACK_TO_MENU]); return
    if command == "ask":
        if args.strip():
            answer_question(update, args)
        else:
            start_ask(update)
        return
    if command == "whoami":
        user = db.get_user(settings.db_path, user_id) if user_id else None
        role = f"роль: <b>{user.role.value}</b>" if user else "роли нет"
        reply(update, f"🆔 Ваш ID в MAX: <code>{user_id}</code>\n{role}")
        return
    if command == "chatid":
        reply(update, f"🆔 ID этого чата: <code>{update.chat_id}</code>")
        return

    # --- служебные ---
    if not is_moderator:
        reply(update,
              "⛔ У вас нет доступа к этой команде.\n\n"
              "Доступные разделы — в меню.", [BACK_TO_MENU])
        return

    if command == "fetch":
        if _moderator_chat_id() is None:
            reply(update, "⚠️ MAX_MODERATOR_CHAT_ID не задан. Узнать id — /chatid")
            return
        reply(update, "⏳ Запускаю сбор новостей, это займёт несколько минут...")
        _worker.submit(run_collection, update.chat_id)
        return

    if command == "pending":
        items = db.get_by_status(settings.db_path, NewsStatus.PENDING_MODERATION)
        if not items:
            reply(update, "✨ Очередь пуста."); return
        reply(update, f"📋 В очереди {len(items)} новостей.")
        for item in items:
            send_to_moderators(item, chat_id=update.chat_id)
            time.sleep(0.4)
        return

    if command == "stats":
        _cmd_stats(update); return
    if command == "audience":
        _cmd_audience(update); return
    if command == "schedule_status":
        state = _get_scheduler_state()
        reply(update,
              f"🕒 <b>Автосбор</b>\n\n"
              f"Состояние: {'включён ✅' if state.get('enabled') else 'выключен ⏸'}\n"
              f"Интервал: {state.get('interval_minutes')} мин.\n"
              f"Последний запуск: {state.get('last_run_at') or 'ещё не было'}")
        return

    if not is_admin:
        reply(update, "⛔ Эта команда доступна только администратору.")
        return

    _handle_admin_command(update, command, args)


def _handle_admin_command(update: MaxUpdate, command: str, args: str) -> None:
    if command == "diag":
        _cmd_diag(update); return

    if command == "schedule_on":
        state = _get_scheduler_state(); state["enabled"] = True
        _set_scheduler_state(state)
        reply(update, f"✅ Автосбор включён, интервал {state['interval_minutes']} мин.")
        return
    if command == "schedule_off":
        state = _get_scheduler_state(); state["enabled"] = False
        _set_scheduler_state(state)
        reply(update, "⏸ Автосбор выключен."); return
    if command == "set_interval":
        if not args.strip().isdigit():
            reply(update, "Использование: /set_interval 120"); return
        minutes = int(args.strip())
        if minutes < MIN_INTERVAL_MINUTES:
            reply(update, f"Минимум {MIN_INTERVAL_MINUTES} минут."); return
        state = _get_scheduler_state(); state["interval_minutes"] = minutes
        _set_scheduler_state(state)
        reply(update, f"✅ Интервал: {minutes} мин."); return

    if command == "add_moderator":
        raw = args.strip()
        if not raw.isdigit():
            reply(update,
                  "Использование: <code>/add_moderator ID</code>\n\n"
                  "ID человек узнаёт командой /whoami в этом боте.")
            return
        db.upsert_user(settings.db_path, BotUser(
            telegram_id=int(raw), role=UserRole.MODERATOR, added_by=update.user_id))
        reply(update, f"✅ {raw} назначен модератором."); return

    if command == "remove_moderator":
        raw = args.strip()
        if not raw.isdigit():
            reply(update, "Использование: /remove_moderator ID"); return
        if int(raw) in settings.bot_admin_ids:
            reply(update, "⛔ Этот админ задан в MAX_ADMIN_IDS."); return
        db.remove_user(settings.db_path, int(raw))
        reply(update, f"✅ Роль снята с {raw}."); return

    if command == "list_moderators":
        users = db.list_users(settings.db_path)
        if not users:
            reply(update, "Список пуст."); return
        lines = [f"{'👑' if u.role == UserRole.ADMIN else '🛡'} "
                 f"<code>{u.telegram_id}</code> — {u.role.value}" for u in users]
        reply(update, "👥 <b>Доступ к боту</b>\n\n" + "\n".join(lines)); return

    if command == "clear":
        pending = db.get_by_status(settings.db_path, NewsStatus.PENDING_MODERATION)
        if not pending:
            reply(update, "🧹 Очередь уже пуста."); return
        reply(update, f"⚠️ В очереди {len(pending)} постов.\n\n"
                      f"Подтвердите: /clear_confirm")
        return
    if command == "clear_confirm":
        count = db.archive_all_pending(settings.db_path)
        reply(update, f"✅ Архивировано: {count}"); return

    if command == "cleanup_images":
        removed = image_pipeline.cleanup_orphan_images(
            db.all_image_paths(settings.db_path))
        reply(update, f"🧹 Удалено изображений: {removed}"); return

    if command == "assistant_log":
        entries = db.recent_assistant_questions(settings.db_path, limit=10)
        if not entries:
            reply(update, "Вопросов пока не было."); return
        for entry in entries:
            reply(update,
                  f"🕒 {entry['asked_at'][:16].replace('T', ' ')} · "
                  f"<code>{entry['telegram_id']}</code>\n\n"
                  f"<b>Вопрос:</b> {html.escape(entry['question'][:300])}\n\n"
                  f"<b>Ответ:</b> {(entry['answer'] or '—')[:1200]}")
            time.sleep(0.3)
        return

    if command == "broadcast":
        if not args.strip():
            reply(update, "Использование: /broadcast текст сообщения"); return
        _worker.submit(_do_broadcast, update, args.strip())
        return

    if command == "add_resource":
        _cmd_add_resource(update, args); return
    if command == "list_resources":
        items = db.list_resources(settings.db_path, category=args.strip() or None)
        if not items:
            reply(update, "Список пуст."); return
        lines = [f"<code>{i.id}</code> | {i.region or 'федеральный'}\n"
                 f"{html.escape(i.name)}\n{html.escape(i.url)}" for i in items]
        for start in range(0, len(lines), 10):
            reply(update, "\n\n".join(lines[start:start + 10]))
            time.sleep(0.3)
        return
    if command == "remove_resource":
        removed = db.remove_resource(settings.db_path, args.strip())
        reply(update, "✅ Удалено." if removed else "❔ Не найдено."); return
    if command == "reseed":
        added = seed_public_resources(update_existing=True)
        reply(update, f"✅ Справочник обновлён. Новых записей: {added}"); return

    if command == "test_publish":
        target = _publish_chat_id()
        if target is None:
            reply(update, "MAX_PUBLISH_CHAT_ID не задан."); return
        sent = bot.send_message(
            chat_id=target,
            text=args.strip() or "Проверка связи: бот «Хороший полицейский».")
        reply(update, "✅ Отправлено в канал." if sent else "❌ Не отправилось.")
        return

    reply(update, "Неизвестная команда. Список — /help")


def _cmd_stats(update: MaxUpdate) -> None:
    by_status = db.count_by_status(settings.db_path)
    rejects = db.count_reject_reasons(settings.db_path, days=30)
    labels = {
        "negative_marker": "негатив о сотруднике",
        "off_format": "не тот формат", "too_short": "короткий текст",
        "routine": "служебная рутина", "not_positive": "не позитивная",
        "not_authentic": "сомнительная достоверность",
        "archived": "архивировано вручную", "llm_or_legacy": "решение ИИ",
    }
    lines = [
        "📊 <b>Статистика</b>\n",
        f"⏳ На модерации: {by_status.get('pending_moderation', 0)}",
        f"📤 Опубликовано: {by_status.get('published', 0)}",
        f"📅 За 7 дней: {db.count_recent(settings.db_path, NewsStatus.PUBLISHED, 7)}",
        f"🚫 Отклонено: {by_status.get('rejected', 0)}",
    ]
    if rejects:
        lines.append("\n<b>Причины отказов за 30 дней</b>")
        lines += [f"   • {labels.get(k, k)}: {v}" for k, v in list(rejects.items())[:8]]
    audience = db.count_audience(settings.db_path)
    lines.append(f"\n👥 Пользователей: {audience['total']}, "
                 f"активны за неделю: {audience['active_week']}")
    reply(update, "\n".join(lines))


def _cmd_audience(update: MaxUpdate) -> None:
    stats = db.count_audience(settings.db_path)
    regions = db.top_audience_regions(settings.db_path, limit=8)
    lines = [
        "👥 <b>Аудитория бота</b>\n",
        f"Всего: <b>{stats['total']}</b>",
        f"Активны за 7 дней: <b>{stats['active_week']}</b>",
        f"Активны за 30 дней: <b>{stats['active_month']}</b>",
        f"Указали регион: {stats['with_region']}",
        f"Заблокировали бота: {stats['blocked']}",
        f"\n💬 Вопросов помощнику за 30 дней: "
        f"<b>{db.count_assistant_usage(settings.db_path, 30)}</b>",
    ]
    if regions:
        lines.append("\n<b>Популярные регионы</b>")
        lines += [f"   • {r} — {n}" for r, n in regions]
    reply(update, "\n".join(lines))


def _cmd_diag(update: MaxUpdate) -> None:
    from pipeline.image_render import CARD_RENDER_AVAILABLE

    missing = validate_settings()
    scheduler = _get_scheduler_state()
    lines = [
        "🔧 <b>Диагностика</b>\n",
        f"{'✅' if settings.max_bot_token else '❌'} Токен MAX",
        f"{'✅' if settings.moderator_chat_id else '❌'} Чат модераторов: "
        f"<code>{settings.moderator_chat_id or '—'}</code>",
        f"{'✅' if settings.publish_chat_id else '❌'} Канал публикации: "
        f"<code>{settings.publish_chat_id or '—'}</code>",
        f"{'✅' if settings.ai_configured else '❌'} AI Provider: "
        f"<code>{html.escape(settings.ai_base_url)}</code>",
        "",
        f"{'✅' if CARD_RENDER_AVAILABLE else '❌'} Рендер карточек (Pillow)",
        f"ℹ️ Удалённая генерация: {image_pipeline.generation_status()}",
        f"{'✅' if settings.assistant_enabled and settings.ai_configured else '❌'} "
        f"ИИ-помощник, лимит {settings.assistant_daily_limit}/сутки",
        "",
        f"🕒 Автосбор: {'включён' if scheduler.get('enabled') else 'выключен'}, "
        f"интервал {scheduler.get('interval_minutes')} мин.",
        f"📡 Источников: RSS {len(settings.rss_sources)}, "
        f"Telegram-каналов {len(settings.telegram_channels)}",
    ]
    if missing:
        lines.append("\n⚠️ <b>Не задано:</b>")
        lines += [f"   • {html.escape(m)}" for m in missing]
    reply(update, "\n".join(lines))


def _cmd_add_resource(update: MaxUpdate, args: str) -> None:
    if "|" not in args:
        reply(update,
              "Использование:\n<code>/add_resource категория | Название | "
              "https://ссылка | Регион</code>\n\nКатегории: "
              + ", ".join(c.value for c in ResourceCategory))
        return
    fields = [p.strip() for p in args.split("|")]
    if len(fields) < 3 or not all(fields[:3]):
        reply(update, "Нужно минимум: категория | Название | Ссылка"); return

    category, name, url = fields[0], fields[1], fields[2]
    region = fields[3] if len(fields) > 3 and fields[3] else None
    if category not in {c.value for c in ResourceCategory}:
        reply(update, f"Неизвестная категория «{html.escape(category)}»."); return
    if not url.startswith(("http://", "https://")):
        reply(update, "Ссылка должна начинаться с http:// или https://"); return

    db.add_resource(settings.db_path, PublicResource(
        id=uuid.uuid4().hex[:12], category=category, region=region,
        name=name, url=url, added_by=update.user_id))
    reply(update, f"✅ Добавлено: {html.escape(name)}")


def _do_broadcast(update: MaxUpdate, text: str) -> None:
    recipients = db.list_audience_ids(settings.db_path)
    if not recipients:
        reply(update, "Аудитория пуста."); return

    sent = failed = 0
    for user_id in recipients:
        if bot.send_message(user_id=user_id, text=text) is not None:
            sent += 1
        else:
            db.mark_audience_blocked(settings.db_path, user_id)
            failed += 1
        time.sleep(0.1)

    reply(update, f"📣 <b>Рассылка завершена</b>\n\n"
                  f"✅ Доставлено: {sent}\n⚠️ Не доставлено: {failed}")


# ==========================================================================
# Разбор событий
# ==========================================================================

def handle_callback(update: MaxUpdate) -> None:
    payload = update.payload or ""
    _track(update)

    def ack(notification: str | None = None) -> None:
        if update.callback_id:
            bot.answer_callback(update.callback_id, notification=notification)

    # --- модерация (только для модераторов) ---
    if payload[:2] in ("a:", "x:", "i:"):
        if not has_role(update.user_id, UserRole.MODERATOR):
            ack("Нет доступа")
            return

        action, item_id = payload[0], payload[2:]
        if action == "a":
            ack(approve_item(update, item_id))
        elif action == "x":
            claimed = db.claim_for_moderation(
                settings.db_path, item_id, NewsStatus.REJECTED, update.user_id)
            ack("Отклонено" if claimed else "Уже обработана")
        else:
            ack("Рисую новую картинку…")
            item = db.get_by_id(settings.db_path, item_id)
            if item and item.status == NewsStatus.PENDING_MODERATION:
                updated = regenerate_image(item_id, True)
                if updated:
                    send_to_moderators(updated, chat_id=update.chat_id)
        return

    ack()

    # --- публичное меню ---
    if payload == "m:home":
        _awaiting_question.discard(update.user_id or 0)
        show_menu(update)
    elif payload == "m:sit":
        show_situations(update)
    elif payload == "m:ph":
        show_phones(update)
    elif payload == "m:ask":
        start_ask(update)
    elif payload == "m:res":
        show_resources_home(update)
    elif payload == "m:news":
        show_news(update)
    elif payload == "m:edu":
        reply(update, EDUCATION_INFO,
              [[callback_button("🎓 Список вузов", "r:edu:0")], BACK_TO_MENU])
    elif payload == "m:reg":
        show_region_picker(update)
    elif payload == "m:about":
        reply(update, ABOUT_TEXT, [BACK_TO_MENU])
    elif payload.startswith("s:"):
        show_situation(update, payload[2:])
    elif payload.startswith("r:"):
        _, key, page = payload.split(":", 2)
        show_resource_category(update, key, int(page))
    elif payload.startswith("rr:"):
        _, key, index = payload.split(":", 2)
        show_resource_region(update, key, int(index))
    elif payload.startswith("gp:"):
        show_region_picker(update, int(payload[3:]))
    elif payload.startswith("g:"):
        set_region(update, int(payload[2:]))
    elif payload == "gc":
        if update.user_id:
            db.set_audience_region(settings.db_path, update.user_id, None)
        reply(update, "📍 Регион сброшен.", [BACK_TO_MENU])
    elif payload == "noop":
        pass
    else:
        logger.debug("Неизвестный payload: %s", payload)


_SMALLTALK = {
    "привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер",
    "спасибо", "благодарю", "спс", "пока", "ок", "окей", "хорошо",
    "понятно", "ясно", "да", "нет", "тест",
}


def handle_message(update: MaxUpdate) -> None:
    text = (update.text or "").strip()
    if not text:
        return

    _track(update)

    if text.startswith("/"):
        parts = text[1:].split(maxsplit=1)
        command = parts[0].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        handle_command(update, command, args)
        return

    # В групповых чатах на обычные реплики не реагируем, иначе бот будет
    # вмешиваться в каждое обсуждение.
    if not update.is_private and update.chat_id != _moderator_chat_id():
        return

    if update.user_id in _awaiting_question:
        answer_question(update, text)
        return

    if text.lower().strip(" .,!?…") in _SMALLTALK:
        reply(update, "Здравствуйте! Выберите раздел или просто напишите вопрос.",
              MENU_BUTTONS)
        return

    if len(text) < assistant.MIN_QUESTION_CHARS:
        reply(update, "Не понял вопрос. Выберите раздел или напишите подробнее.",
              MENU_BUTTONS)
        return

    if settings.assistant_enabled and settings.ai_configured:
        answer_question(update, text)
    else:
        reply(update, "Выберите раздел:", MENU_BUTTONS)


def handle_update(update: MaxUpdate) -> None:
    if update.type == "message_callback":
        handle_callback(update)
    elif update.type == "message_created":
        handle_message(update)
    elif update.type == "bot_started":
        _track(update)
        show_menu(update)
    elif update.type == "bot_added":
        logger.info("Бот добавлен в чат %s", update.chat_id)
        if update.chat_id is not None:
            bot.send_message(
                chat_id=update.chat_id,
                text=f"👮 Бот подключён.\nID этого чата: <code>{update.chat_id}</code>",
            )
    elif update.type in ("bot_stopped", "bot_removed"):
        if update.user_id:
            db.mark_audience_blocked(settings.db_path, update.user_id)
    else:
        logger.debug("Событие без обработчика: %s", update.type)


# ==========================================================================
# Запуск
# ==========================================================================

# Команды для меню MAX задаются вручную у @MasterBot (/commands) —
# API их менять не позволяет. Список для копирования:
#
#   menu - Главное меню
#   situations - Что делать, если…
#   phones - Экстренные телефоны
#   ask - Задать вопрос
#   resources - Официальные источники МВД
#   news - Хорошие новости
#   education - Учёба в МВД
#   region - Выбрать свой регион
#   about - О проекте
#   help - Справка


def poll_forever() -> None:
    logger.info("Начинаю опрос событий MAX...")
    while True:
        try:
            for update in bot.get_updates(timeout=30):
                try:
                    handle_update(update)
                except Exception:
                    logger.exception("Ошибка обработки события %s", update.type)
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("Ошибка цикла опроса")
            time.sleep(5)


def main() -> None:
    global bot

    if not settings.max_bot_token:
        logger.error(
            "MAX_BOT_TOKEN не задан — бот не может запуститься.\n"
            "Скопируйте .env.example в .env и заполните значения."
        )
        sys.exit(1)

    validate_settings()
    db.init_db(settings.db_path)
    bootstrap_admins()
    seed_public_resources()

    bot = MaxBot(settings.max_bot_token)

    me = bot.get_me()
    logger.info("Бот MAX «%s» (@%s, id=%s) запущен",
                me.get("name"), me.get("username"), me.get("user_id"))

    threading.Thread(target=scheduler_loop, daemon=True,
                     name="scheduler").start()

    try:
        poll_forever()
    finally:
        from pipeline.ai_client import close_client
        close_client()
        _worker.shutdown(wait=False)
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено пользователем.")
