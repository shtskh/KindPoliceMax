"""
Единая точка вычисления путей проекта.

Зачем отдельный модуль: раньше пути вроде "storage/images" и "storage/bot.db"
были относительными, то есть зависели от текущей рабочей директории. Если бота
запускали не из корня проекта (systemd, планировщик Windows, IDE с другим
working dir), картинки сохранялись в одно место, а бот искал их в другом —
os.path.exists(item.image_path) возвращал False и пост уходил без фото.

Теперь все пути считаются от расположения этого файла, поэтому работают
одинаково независимо от того, откуда запущен процесс.
"""
from __future__ import annotations

import os
from pathlib import Path

# Корень проекта — папка, в которой лежит этот файл.
PROJECT_ROOT = Path(__file__).resolve().parent

# Папка данных — единственное место, которое переживает пересборку
# образа на хостинге. Всё остальное в контейнере собирается заново,
# поэтому база и картинки, лежащие внутри проекта, исчезали при каждом
# деплое: слетали роли модераторов, очередь и иллюстрации уже
# опубликованных постов.
#
# Путь к такой папке хостинг передаёт в переменной DATA_DIR. Переменной
# нет — работаем как раньше, прямо в проекте: на домашней машине
# отдельная папка данных только мешала бы.
_DATA_DIR_RAW = os.getenv("DATA_DIR", "").strip()
DATA_DIR = (Path(_DATA_DIR_RAW).expanduser().resolve()
            if _DATA_DIR_RAW else PROJECT_ROOT)

STORAGE_DIR = DATA_DIR / "storage"
IMAGES_DIR = STORAGE_DIR / "images"
# Шрифты и прочее содержимое образа данными не являются: они приезжают
# вместе со сборкой и в папке данных им делать нечего.
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
LOGS_DIR = PROJECT_ROOT / "logs"

DEFAULT_DB_PATH = STORAGE_DIR / "bot.db"


def ensure_dirs() -> None:
    """Создаёт рабочие директории. Идемпотентно, зовётся при старте."""
    for directory in (STORAGE_DIR, IMAGES_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def resolve(path_like: str | Path) -> Path:
    """
    Приводит путь к абсолютному: относительные пути считаются от корня
    проекта, абсолютные остаются как есть.

    Нужно для обратной совместимости — в БД уже лежат записи с путями
    вида "storage/images/123.jpg", записанные старой версией кода.
    """
    path = Path(path_like)
    if path.is_absolute():
        return path

    current = (DATA_DIR / path).resolve()
    if DATA_DIR == PROJECT_ROOT or current.exists():
        return current
    # Запись сделана до переезда в папку данных: картинка может лежать
    # ещё внутри проекта. Отдаём её, пока файл на месте, — иначе пост
    # на витрине остался бы без иллюстрации.
    legacy = (PROJECT_ROOT / path).resolve()
    return legacy if legacy.exists() else current


def to_relative(path_like: str | Path) -> str:
    """
    Обратное преобразование — храним в БД путь относительно папки
    данных. Так базу можно перенести на другой сервер вместе с папкой
    storage, и пути не сломаются (в отличие от абсолютных
    C:\\Users\\...). Заодно записи остаются читаемыми, если папку
    данных потом подключат по другому пути.
    """
    path = Path(path_like).resolve()
    for base in (DATA_DIR, PROJECT_ROOT):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()
