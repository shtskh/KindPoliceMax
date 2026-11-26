"""
Составление промпта для генератора изображений по тексту новости.

ЗАЧЕМ ОТДЕЛЬНЫЙ ШАГ
-------------------
Подставлять текст новости в генератор напрямую бесполезно: модели
рисуют по визуальному описанию, а новость написана канцелярским языком
про процессуальные действия. На запрос «сотрудники ОМВД в рамках
профилактического мероприятия оказали содействие» генератор нарисует
что угодно.

Поэтому сцену сочиняет языковая модель (она у провайдера работает, в
отличие от генерации картинок): читает новость и описывает КАДР —
место, время суток, погоду, что происходит в этот момент.

ГРАНИЦЫ, КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ
---------------------------------
Тема ведомственная, поэтому в промпте жёстко запрещено:

  * узнаваемые лица — это не фотография конкретных людей, и выдавать
    нарисованных людей за участников события нельзя;
  * точное воспроизведение формы, шевронов, гербов и номеров машин —
    официальная символика не должна подделываться;
  * текст и надписи на изображении — генераторы пишут бессмыслицу,
    а на ведомственной картинке это выглядит особенно скверно;
  * кровь, травмы, тела, оружие в действии — канал про добрые дела.

Сгенерированная картинка в любом случае помечается надписью
«Иллюстрация, сгенерировано ИИ» (см. pipeline/image_render.py).
"""
from __future__ import annotations

import hashlib
import re

from config import settings
from logging_setup import get_logger
from pipeline.ai_client import AIProviderError, chat
from pipeline.ranking import is_prevention

logger = get_logger("image_prompt")

# Сколько текста новости отдаём модели: сцена берётся из первых абзацев,
# дальше обычно идут процессуальные подробности.
MAX_NEWS_CHARS = 1200

# Потолок длины промпта. Генераторы обрезают длинные описания, а лишние
# детали размывают кадр.
MAX_PROMPT_CHARS = 900

SYSTEM_PROMPT = """
Ты — художник-постановщик, который по новости придумывает ОДИН кадр
для иллюстрации к посту.

Верни описание кадра на АНГЛИЙСКОМ языке, одним абзацем, 40-70 слов.
Только описание сцены, без пояснений и без кавычек.

ГЛАВНОЕ ТРЕБОВАНИЕ — КАДР ИЗ ЭТОЙ НОВОСТИ, А НЕ ВООБЩЕ
Найди в тексте предмет, место или момент, по которым новость узнаётся:
велосипед, забор, полынью, банкомат, школьный класс, перевёрнутую
машину, телефонную трубку. Именно это и должно быть в кадре.
Иллюстрация, которая подошла бы к любой другой новости, не годится.

ЗАПРЕЩЁННЫЙ ШТАМП
Двое сотрудников со спины идут по улице или по парку — этот кадр уже
был сотни раз. Не предлагай его, если в новости речь не идёт прямо о
патрулировании. Людей в кадре может не быть вовсе.

ЧТО ОПИСЫВАТЬ
- Место действия из новости: двор, трасса, подъезд, берег реки, лесная
  тропа, рынок, школьный коридор, дежурная часть, кабинет, вокзал,
  автобусная остановка, гараж, кухня в квартире, больничный коридор.
- Время суток и погоду, если они понятны из текста.
- Что происходит в кадре: действие или след действия, а не портрет.
- Атмосферу: спокойная, рабочая, человечная.
- Русские реалии: панельные дома, «Лада», берёзы, зимняя слякоть.

РАКУРС
Ракурс и свет заданы в задании ниже. Следуй им, если они не
противоречат новости; если противоречат — следуй новости.

ОБЯЗАТЕЛЬНЫЕ ОГРАНИЧЕНИЯ (нарушать нельзя)
- Люди только со спины, издалека или не в фокусе. Никаких узнаваемых
  лиц и портретов крупным планом.
- Форму не придумывай и не сокращай: как одеты сотрудники, сказано
  ниже в задании — повтори это описание своими словами целиком, вместе
  с нашивками. {insignia_rule}
- Если в задании сказано, что сотрудников в кадре нет, — не добавляй
  их и не описывай форму вовсе.
- Не описывай номера машин.
- Никакой крови, травм, тел, оружия в действии, огнестрельных ран.
- Не придумывай деталей, которых нет в новости.

СТИЛЬ
Документальная фотография, естественное освещение, спокойные цвета.

Если по новости кадр придумать невозможно (отчёт, статистика,
поздравление) — верни ровно одно слово: NONE
""".strip()

# Как описывать форму. Проверено живыми генерациями 10.11.2026:
# крой и цвет формы МВД генератор передаёт верно (тёмно-синий китель,
# фуражка с кокардой, погоны), а вот шеврон подделывает — надпись «МВД»
# на нём превращается в нечитаемый набор букв, а эмблема получается
# лишь похожей на настоящую. При просмотре в ленте это незаметно, при
# увеличении — сразу видно.
#
# Отдельная беда — спина. Генераторы обучены на снимках старого
# образца, где на спине куртки написано «ПОЛИЦИЯ»; действующий образец —
# «МВД РОССИИ». А если про нашивку не сказать вовсе, спина выходит
# пустой, чего на форменной куртке не бывает.
#
# Поэтому нашивки описываем всегда — как деталь кроя: прямоугольник в
# красной окантовке через всю спину, круглый шеврон на левом рукаве,
# погоны. Читаемость надписей на них — отдельный вопрос, им и
# управляет переключатель.
#
# По умолчанию форма узнаваемая, но символика не читается.
# Переключатель AI_IMAGE_SHOW_INSIGNIA=true разрешает генератору
# рисовать шевроны и кокарды крупно — со всеми их неточностями.
_INSIGNIA_RULE_OFF = (
    "Нашивки с формы не убирай — они на ней есть, — но опиши кадр так, "
    "чтобы надписи на них не читались: вид со спины, с расстояния или "
    "под углом."
)
_INSIGNIA_RULE_ON = (
    "Шеврон на рукаве, кокарду и нашивку на спине описывай так, как "
    "сказано в задании: на спине действующий образец «МВД РОССИИ», "
    "а не старый «ПОЛИЦИЯ»."
)

_SUFFIX_BASE = (
    "Documentary photography, natural light, muted colors, candid moment. "
    "No recognizable faces, no close-up portraits. "
    "No blood, no injuries, no weapons."
)
# Общая часть про символику: экипировку подставляет detect_unit,
# а вот знаки различия в любом случае остаются неразборчивыми.
_SUFFIX_NO_INSIGNIA = (
    "Seen from behind or at a distance. The uniform is complete: "
    "shoulder boards, the round patch high on the left sleeve and the "
    "wide patch across the upper back are all in place — the back of a "
    "uniform jacket is never blank. At this distance the emblems and "
    "lettering stay small, soft and not legible. "
    "No readable text, no logos, no watermarks."
)
_SUFFIX_WITH_INSIGNIA = (
    "Peaked cap with cockade, shoulder boards, the round emblem patch "
    "high on the left sleeve, and the wide rectangular back patch "
    "between the shoulder blades lettered МВД РОССИИ in white on dark "
    "fabric with a red border — the current pattern, never the older "
    "ПОЛИЦИЯ back patch. No other lettering, no watermarks."
)


# --------------------------------------------------------------------------
# Разнообразие кадров
# --------------------------------------------------------------------------
#
# Предоставленный сам себе генератор рисует один и тот же кадр: двое
# сотрудников со спины идут по осенней улице. Виноват не он — виноват
# промпт, в котором не было ни ракурса, ни точки съёмки, зато было
# «документальная фотография, люди со спины». В ленте из двадцати постов
# однообразие заметно сильнее любой неточности в форме.
#
# Поэтому ракурс и свет выбираются заранее и уходят в задание как
# требование. Выбор детерминированный, по хешу новости: один и тот же
# пост при повторной генерации (/regen_image) даст тот же тип кадра,
# а соседние посты в ленте — разные.

_FRAMING_VARIANTS: tuple[tuple[str, str], ...] = (
    ("крупный план главного предмета из новости; люди только размытыми "
     "силуэтами на заднем плане или их нет вовсе",
     "Tight close-up of the key object from the story, shallow depth of "
     "field, any people only as blurred shapes far behind."),
    ("вид из салона автомобиля через лобовое стекло, часть панели в кадре",
     "Shot from inside a car through the windscreen, part of the "
     "dashboard visible in the foreground."),
    ("общий план сверху, с высоты третьего-четвёртого этажа",
     "High angle from a third-floor window, wide establishing shot of "
     "the yard below."),
    ("кадр через дверной проём или оконную раму, рама в кадре",
     "Framed through a doorway or a window frame, the frame dark in "
     "the foreground."),
    ("только руки и предмет, лиц в кадре нет вовсе",
     "Hands and an object only, cropped at the wrists, no faces in "
     "frame at all."),
    ("силуэты против света, длинные тени, контровой свет",
     "Backlit silhouettes, long shadows, strong rim light."),
    ("интерьер: дежурная часть, кабинет, подъезд, школьный коридор",
     "Interior scene — a duty room, an office, a stairwell or a school "
     "corridor — under fluorescent light."),
    ("низкая точка съёмки, камера у самой земли",
     "Very low camera angle, lens close to the ground, foreground "
     "detail large in frame."),
    ("отражение в витрине, стекле автомобиля или луже",
     "The scene seen as a reflection in a shop window, a car window or "
     "a puddle."),
    ("средний план со спины, человек уходит вглубь кадра",
     "Medium shot from behind, the subject walking away into the depth "
     "of the frame."),
)

_LIGHT_VARIANTS: tuple[tuple[str, str], ...] = (
    ("раннее утро, холодный свет",
     "cold early-morning light, long blue shadows"),
    ("пасмурный день, мягкий рассеянный свет",
     "overcast day, soft diffused light, no shadows"),
    ("вечер, уличные фонари",
     "evening, warm street lamps against a deep blue sky"),
    ("ясный день, резкие тени",
     "clear day, hard sunlight and sharp shadows"),
    ("сумерки, синий час, светящиеся окна",
     "blue hour, lit windows against dark blue air"),
    ("ночь, мокрый асфальт, отражения огней",
     "night, wet asphalt, reflections of lights on the road"),
)

# Кадры для предупреждений о мошенничестве. Сотрудников здесь обычно
# нет вовсе: новость не о том, как кого-то спасли, а о том, как устроен
# обман, и кадр должен показывать его примету — телефон с незнакомым
# номером, конверт с деньгами, банкомат.
_WARNING_SCENES: tuple[tuple[str, str], ...] = (
    ("экран смартфона с входящим вызовом с неизвестного номера, "
     "телефон в руке, лица нет",
     "A smartphone held in a hand showing an incoming call from an "
     "unknown number, the screen glow on the fingers, no face in frame."),
    ("банковская карта и остывший чай на кухонном столе, вечер",
     "A bank card on a kitchen table beside a cold cup of tea, evening "
     "light through the window."),
    ("пожилой человек со спины у окна со старым телефоном в руке",
     "An elderly person seen from behind at a window, an old landline "
     "handset in hand."),
    ("ноутбук в тёмной комнате, клавиатура подсвечена экраном",
     "An open laptop in a dark room, the keyboard lit only by the "
     "screen, an empty chair beside it."),
    ("дверь квартиры изнутри, дверной глазок, тусклый свет прихожей",
     "The inside of a flat door with a peephole, dim hallway light, "
     "coats on a hook."),
    ("банкомат в освещённом вестибюле, человек со спины",
     "A cash machine in a lit bank lobby, a person seen from behind."),
    ("плотный конверт с купюрами на столе рядом с сумкой курьера",
     "A thick envelope of banknotes on a table next to a courier bag."),
    ("подъезд жилого дома в сумерках, панель домофона крупно",
     "An apartment building entrance at dusk, the intercom panel large "
     "in the foreground."),
)

# Если сотрудников в кадре быть не должно.
_UNIT_NONE = (
    "No uniformed officers and no patrol cars in the frame at all — "
    "this is an object-led illustration."
)


def _pick(item, variants: tuple, salt: str):
    """Устойчивый выбор варианта по новости: одна новость — один кадр."""
    key = (getattr(item, "id", "") or getattr(item, "source_url", "")
           or getattr(item, "title", "") or "")
    digest = hashlib.sha1((salt + str(key)).encode("utf-8")).hexdigest()
    return variants[int(digest[:8], 16) % len(variants)]


# Какой предметный кадр к какой схеме обмана. Без этой привязки хеш
# ставил к новости про код из СМС кадр с дверным глазком: формально
# разнообразно, по сути мимо. Порядок важен — берётся первое
# совпадение, поэтому самые характерные приметы стоят выше.
_WARNING_HINTS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("код из смс", "назвать код", "позвонил", "звонят", "трубк",
      "телефонн"), 0),
    (("реквизит", "банковск карт", "банковской карт", "безопасный счёт",
      "безопасный счет", "перевела", "перевёл", "перевел"), 1),
    (("пенсионер", "пожил", "бабушк", "ветеран", "престарел"), 2),
    (("ссылк", "фишинг", "сайт", "аккаунт", "пароль", "госуслуг",
      "авторизац", "вредоносн"), 3),
    (("дверь", "квартир", "пришли домой", "под видом сотрудника соцзащиты"), 4),
    (("банкомат", "сняла накоплени", "снял накоплени", "наличн"), 5),
    (("курьер", "конверт", "передать деньги", "передала деньги"), 6),
    (("подъезд", "домофон", "сосед"), 7),
)


def _warning_scene(item):
    """Предметный кадр под конкретную схему, иначе — по хешу."""
    haystack = " ".join(
        str(part).lower() for part in
        (getattr(item, "title", ""), getattr(item, "raw_text", ""))
        if part
    )
    for words, index in _WARNING_HINTS:
        if any(word in haystack for word in words):
            return _WARNING_SCENES[index]
    return _pick(item, _WARNING_SCENES, "warn")


def wants_warning_frame(item) -> bool:
    """
    Новость-предупреждение иллюстрируется предметом, а не людьми.

    Категорию проставляет верификация (pipeline/verify.py), но она
    попадает в заметки модератора, а не в отдельное поле новости, и при
    повторной генерации картинки заметок может не оказаться. Поэтому
    проверяем три признака подряд.
    """
    notes = (getattr(item, "verification_notes", "") or "").lower()
    if "категория: предупреждение" in notes:
        return True
    tags = [str(t).lower() for t in (getattr(item, "tags", None) or [])]
    if any(t in ("мошенничество", "кибербезопасность", "профилактика")
           for t in tags):
        return True
    return is_prevention(item)


def prompt_suffix(unit_look: str | None = None, *,
                  with_officers: bool = True) -> str:
    """
    Хвост, который добавляется к любому промпту: повторяет ключевые
    требования на языке генератора, потому что часть моделей следует
    финальным указаниям охотнее, чем середине описания.

    with_officers=False — предметный кадр без сотрудников: описывать
    форму в таком хвосте нельзя, иначе генератор всё равно втащит в
    кадр человека в кителе.
    """
    if not with_officers:
        return (f"{_SUFFIX_BASE} {_UNIT_NONE} "
                "No readable text, no logos, no watermarks.")
    look = unit_look or _UNIT_DEFAULT
    tail = (_SUFFIX_WITH_INSIGNIA if settings.ai_image_show_insignia
            else _SUFFIX_NO_INSIGNIA)
    return f"{_SUFFIX_BASE} {look} {tail}"


def system_prompt() -> str:
    rule = (_INSIGNIA_RULE_ON if settings.ai_image_show_insignia
            else _INSIGNIA_RULE_OFF)
    return SYSTEM_PROMPT.format(insignia_rule=rule)

# Запасного шаблона с фиксированной сценой здесь больше нет: когда
# модель недоступна, кадр собирается из того же набора ракурсов, что и
# обычный промпт (см. конец build_image_prompt). Прежний шаблон
# «улица, люди со спины» давал ровно тот однообразный кадр, от
# которого мы и уходим.

_NONE_RE = re.compile(r"^\s*none\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Форма по службам
# --------------------------------------------------------------------------
#
# «Тёмно-синяя форма» для всех подряд — неправда: инспектор ДПС без
# салатового жилета со светоотражающими полосами на снимке с дороги не
# похож на себя, а участковый в бронежилете похож на патрульного.
# Поэтому служба определяется по тексту новости, и генератору уходит
# описание именно её экипировки.
#
# Признаки взяты по открытым материалам пресс-служб МВД: у ДПС это
# салатовый жилет с широкими серыми светоотражающими полосами поверх
# тёмной куртки и патрульный автомобиль с сине-белой окраской, у ППС —
# тёмный бронежилет и рация, у участкового — повседневная форма с
# папкой, у кинолога — полевая форма и служебная собака.
#
# Знаки различия по-прежнему остаются вне фокуса: ведомственную
# символику генератор воспроизводит неточно (см. комментарий ниже).

_UNIT_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("дпс", "госавтоинспек", "автоинспек", "гибдд", "инспектор дорожн",
         "дорожно-патрульн", "на трассе", "на дороге остановил"),
        "ДПС",
        "Traffic police officers in dark navy uniform wearing "
        "high-visibility lime-yellow safety vests with wide grey "
        "reflective stripes over the jacket, peaked caps; the vest "
        "carries a dark lettered panel across the back between the "
        "reflective stripes; a white and blue patrol car with a blue "
        "light bar nearby.",
    ),
    (
        ("патрульно-постов", "ппс", "патрульный наряд", "наряд полиции",
         "патрулировани"),
        "ППС",
        "Patrol officers in dark navy uniform with dark protective vests "
        "over the jacket and a radio on the shoulder, peaked caps; the "
        "jacket has a round patch high on the left sleeve and a wide "
        "rectangular patch with a red border across the upper back, on "
        "foot in a city street.",
    ),
    (
        ("участков", "уполномоченный полиции"),
        "участковый",
        "A district police officer in everyday dark navy uniform — tunic "
        "or shirt with shoulder boards and a round patch on the left "
        "sleeve — peaked cap and a document folder, no tactical gear and "
        "no back patch, because the indoor uniform has none, at the "
        "entrance of a residential building.",
    ),
    (
        ("кинолог", "служебная собака", "служебной собаки", "овчарк"),
        "кинолог",
        "A police dog handler in dark navy field uniform — jacket with a "
        "round patch high on the left sleeve and a wide rectangular "
        "patch with a red border across the upper back, trousers tucked "
        "into boots — with a German shepherd on a lead.",
    ),
    (
        ("следовател", "уголовного розыска", "оперуполномоч", "сыщик"),
        "розыск",
        "Plain-clothes officers in dark civilian jackets, no uniform "
        "details visible.",
    ),
    (
        ("курсант", "вуз мвд", "института мвд", "университета мвд",
         "академии мвд", "слушател"),
        "курсанты",
        "Cadets of a police academy in dark navy dress uniform — "
        "single-breasted tunic with shoulder boards, white shirt and "
        "tie, a round patch on the left sleeve and no back patch, "
        "because the dress uniform has none — peaked caps, lined up "
        "outdoors in front of an institute building.",
    ),
)

# Если службу определить не удалось.
_UNIT_DEFAULT = (
    "Officers in dark navy blue Russian police uniform: peaked cap with "
    "cockade, shoulder boards, a round patch high on the left sleeve "
    "and, on the outdoor jacket or parka, a wide rectangular patch with "
    "a red border across the upper back."
)


def detect_unit(item) -> tuple[str | None, str]:
    """
    Служба из текста новости и описание её экипировки для генератора.

    Возвращает (название службы или None, описание на английском).
    """
    haystack = " ".join(
        part.lower() for part in
        (getattr(item, "title", ""), getattr(item, "raw_text", ""),
         getattr(item, "rewritten_text", "") or "")
        if part
    )
    for keywords, name, description in _UNIT_RULES:
        if any(word in haystack for word in keywords):
            return name, description
    return None, _UNIT_DEFAULT


def _season_hint(month: int) -> str:
    if month in (12, 1, 2):
        return "winter, snow on the ground"
    if month in (3, 4, 5):
        return "early spring, wet pavement"
    if month in (6, 7, 8):
        return "summer, green trees"
    return "autumn, yellow leaves"


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.strip('"').strip("'").strip()
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS].rsplit(" ", 1)[0]
    return text


def build_image_prompt(item) -> str | None:
    """
    Описание кадра для генератора. None — если иллюстрировать нечего
    (отчёт, статистика) или модель недоступна и запасной вариант
    отключён настройкой.
    """
    source_text = (item.raw_text or item.title or "").strip()
    if not source_text:
        return None

    month = getattr(getattr(item, "published_at", None), "month", 6) or 6

    # Предупреждение о мошенничестве иллюстрируется предметом, а не
    # людьми: сотрудников в такой новости обычно нет вовсе.
    warning = wants_warning_frame(item)

    # Экипировка зависит от службы: инспектор ДПС без
    # салатового жилета на снимке с дороги на себя не похож.
    unit, unit_look = detect_unit(item)
    suffix = prompt_suffix(unit_look, with_officers=not warning)

    framing_ru, framing_en = (_warning_scene(item) if warning
                              else _pick(item, _FRAMING_VARIANTS, "frame"))
    light_ru, light_en = _pick(item, _LIGHT_VARIANTS, "light")

    if not settings.ai_configured:
        logger.info("Нет ключа AI — беру обобщённое описание кадра.")
        return (f"{framing_en} {light_en}. "
                f"A Russian town, {_season_hint(month)}. {suffix}")

    place = " · ".join(p for p in (getattr(item, "city", None),
                                   getattr(item, "region", None)) if p)
    dress = (
        "Сотрудников в кадре нет — это предметный кадр."
        if warning else f"Как одеты сотрудники: {unit_look}"
    )
    user_prompt = (
        f"Место события: {place or 'не указано'}\n"
        f"Время года: {_season_hint(month)}\n"
        f"Ракурс и композиция (следуй этому): {framing_ru}\n"
        f"Свет: {light_ru}\n"
        f"{dress}\n\n"
        f"Новость:\n{source_text[:MAX_NEWS_CHARS]}"
    )

    try:
        scene = chat(
            [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            model=settings.ai_model,
            max_tokens=300,
            # Было 0.6 — при нём модель раз за разом выдавала один и тот
            # же кадр с двумя сотрудниками на улице. Ракурс теперь задан
            # заданием, и более высокая температура уже не уводит кадр
            # от новости, а только меняет детали.
            temperature=0.9,
            label="image_prompt",
        )
    except AIProviderError as exc:
        logger.warning("Не удалось сочинить кадр (%s) — беру обобщённый.", exc)
        return (f"{framing_en} {light_en}. "
                f"A Russian town, {_season_hint(month)}. {suffix}")

    scene = _clean(scene)

    if not scene or _NONE_RE.match(scene):
        logger.info("Модель решила, что иллюстрировать нечего — будет карточка.")
        return None

    prompt = f"{scene} {light_en}. {suffix}"
    logger.info("Кадр для генерации (%s, %s): %s",
                "предупреждение" if warning else (unit or "служба не определена"),
                framing_ru[:28], scene[:120])
    return prompt
