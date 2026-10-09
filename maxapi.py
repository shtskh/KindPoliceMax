"""
Клиент Bot API мессенджера MAX.

Аналога aiogram для MAX нет, поэтому тонкая обёртка над HTTP пишется
руками. Здесь только транспорт: отправка, правка, загрузка картинок,
длинный опрос событий. Вся логика бота — в max_bot.py.

ЧТО ПРОВЕРЕНО НА ЖИВОМ ТОКЕНЕ (документация местами отстаёт):

  1. Базовый адрес — https://botapi.max.ru. Встречающийся в документации
     platform-api2.max.ru не отвечает вовсе.
  2. Токен передаётся заголовком «Authorization: <токен>», БЕЗ слова
     Bearer: с префиксом приходит 401 «Malformed access token», а
     старый параметр ?access_token= отвечает 401 «deprecated».
  3. Получатель задаётся в query: /messages?chat_id=… или ?user_id=…
     Без него — 400 «Unknown recipient».
  4. GET /chats боту недоступен (404 method.not.found), поэтому узнать
     chat_id канала можно только из входящих событий.
  5. POST /uploads?type=image отдаёт {"url": "…"} — временный адрес,
     куда нужно залить файл вторым запросом.
  6. Ответ на нажатие кнопки — POST /answers?callback_id=…

ОТЛИЧИЯ ОТ TELEGRAM, ВАЖНЫЕ ДЛЯ ИНТЕРФЕЙСА:

  * Постоянной клавиатуры снизу (ReplyKeyboard) в MAX нет — меню
    делается inline-кнопками, прикреплёнными к сообщению.
  * Текст и кнопки — это одно поле attachments, клавиатура прилетает
    вложением типа inline_keyboard.
  * Лимит текста сообщения — 4000 символов (в Telegram 4096).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from logging_setup import get_logger

logger = get_logger("maxapi")

MAX_API_BASE = os.getenv("MAX_API_BASE", "https://botapi.max.ru").rstrip("/")

# Лимит текста сообщения в MAX.
MESSAGE_LIMIT = 4000

# Коды, при которых есть смысл повторить запрос.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class MaxApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------
# Кнопки
# --------------------------------------------------------------------------

def callback_button(text: str, payload: str) -> dict:
    """Кнопка, которая присылает боту событие message_callback."""
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}


def keyboard(rows: Iterable[Iterable[dict]]) -> dict:
    """
    Клавиатура прикрепляется к сообщению вложением, а не отдельным
    полем reply_markup, как в Telegram.
    """
    return {
        "type": "inline_keyboard",
        "payload": {"buttons": [list(row) for row in rows if list(row)]},
    }


# --------------------------------------------------------------------------
# Разобранные события
# --------------------------------------------------------------------------

@dataclass
class MaxUpdate:
    """Событие от MAX, приведённое к удобному виду."""
    type: str
    raw: dict = field(repr=False, default_factory=dict)

    chat_id: int | None = None
    user_id: int | None = None
    user_name: str = ""
    username: str | None = None
    text: str = ""
    message_id: str | None = None
    callback_id: str | None = None
    payload: str | None = None
    is_channel: bool = False
    chat_type: str = ""

    @property
    def is_private(self) -> bool:
        """Личная переписка с ботом (в MAX это chat_type == 'dialog')."""
        return self.chat_type in ("dialog", "", "private")


def _dig(node: Any, key: str) -> Any:
    """
    Ищет первое значение по ключу на любой глубине.

    Структура событий MAX в документации описана неполно и отличается
    между типами событий, поэтому надёжнее искать поле, а не полагаться
    на фиксированный путь: иначе бот молча перестанет понимать события
    после изменения формата.
    """
    if isinstance(node, dict):
        if key in node and node[key] is not None:
            return node[key]
        for value in node.values():
            found = _dig(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _dig(value, key)
            if found is not None:
                return found
    return None


def parse_update(raw: dict) -> MaxUpdate:
    update = MaxUpdate(type=raw.get("update_type", "unknown"), raw=raw)

    message = raw.get("message") or {}
    recipient = message.get("recipient") or {}
    sender = message.get("sender") or {}
    body = message.get("body") or {}
    callback = raw.get("callback") or {}
    user = callback.get("user") or raw.get("user") or sender or {}

    update.chat_id = (
        recipient.get("chat_id")
        or raw.get("chat_id")
        or _dig(raw, "chat_id")
    )
    update.chat_type = recipient.get("chat_type") or raw.get("chat_type") or ""
    update.user_id = user.get("user_id") or _dig(raw, "user_id")
    update.user_name = (
        user.get("name") or user.get("first_name") or ""
    )
    update.username = user.get("username")
    update.text = (body.get("text") or raw.get("text") or "").strip()
    update.message_id = body.get("mid") or _dig(raw, "mid")
    update.callback_id = callback.get("callback_id")
    update.payload = callback.get("payload")
    update.is_channel = bool(raw.get("is_channel"))

    # Для личной переписки получателем выступает сам пользователь:
    # отвечать нужно по user_id, chat_id там может отсутствовать.
    if update.chat_id is None and recipient.get("user_id"):
        update.chat_id = None
    return update


# --------------------------------------------------------------------------
# Клиент
# --------------------------------------------------------------------------

class MaxBot:
    def __init__(self, token: str, api_base: str = MAX_API_BASE):
        if not token:
            raise ValueError("MAX_BOT_TOKEN не задан")
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        # Именно без «Bearer» — см. шапку модуля.
        self.session.headers.update({"Authorization": token})
        self._marker: int | None = None

    # -- низкий уровень -----------------------------------------------------

    def _request(
        self, method: str, path: str, *, params: dict | None = None,
        payload: dict | None = None, timeout: float = 30.0, retries: int = 3,
    ) -> dict:
        url = f"{self.api_base}{path}"
        last_error = "неизвестная ошибка"

        for attempt in range(1, retries + 1):
            try:
                response = self.session.request(
                    method, url, params=params, json=payload, timeout=timeout
                )
            except requests.RequestException as exc:
                last_error = f"сеть: {type(exc).__name__}: {exc}"
                logger.warning("[%s %s] попытка %d: %s", method, path, attempt, last_error)
                time.sleep(min(2 ** attempt, 15))
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return {}

            try:
                detail = response.json().get("message") or response.text[:200]
            except ValueError:
                detail = response.text[:200]
            last_error = f"HTTP {response.status_code}: {detail}"

            if response.status_code not in RETRYABLE_STATUS:
                raise MaxApiError(f"[{path}] {last_error}", response.status_code)

            logger.warning("[%s %s] попытка %d: %s", method, path, attempt, last_error)
            time.sleep(min(2 ** attempt, 15))

        raise MaxApiError(f"[{path}] исчерпаны попытки. {last_error}")

    # -- информация о боте --------------------------------------------------

    def get_me(self) -> dict:
        return self._request("GET", "/me")

    # Списка команд через API в MAX нет: GET /me только читает профиль,
    # а PATCH/POST/PUT /me отвечают 404 «Path /me is not recognized».
    # Команды для меню задаются вручную у @MasterBot (/commands).

    # -- отправка -----------------------------------------------------------

    def send_message(
        self,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
        text: str,
        buttons: list[list[dict]] | None = None,
        image_path: str | Path | None = None,
        notify: bool = True,
    ) -> dict | None:
        """
        Отправляет сообщение в чат (chat_id) или в личку (user_id).
        Возвращает ответ API или None, если отправить не удалось.
        """
        if chat_id is None and user_id is None:
            logger.error("send_message без получателя")
            return None

        attachments: list[dict] = []

        if image_path:
            attachment = self.upload_image(image_path)
            if attachment:
                attachments.append(attachment)

        if buttons:
            attachments.append(keyboard(buttons))

        body: dict = {
            "text": _trim(text, MESSAGE_LIMIT),
            "format": "html",
            "notify": notify,
        }
        if attachments:
            body["attachments"] = attachments

        params = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}

        try:
            return self._request("POST", "/messages", params=params, payload=body)
        except MaxApiError as exc:
            # Чаще всего это невалидная разметка — пробуем без неё,
            # чтобы сообщение всё-таки дошло.
            logger.warning("Отправка в MAX не удалась (%s), повтор без разметки", exc)
            body.pop("format", None)
            body["text"] = _strip_tags(body["text"])
            try:
                return self._request("POST", "/messages", params=params, payload=body)
            except MaxApiError as exc2:
                logger.error("Сообщение в MAX не доставлено: %s", exc2)
                return None

    def edit_message(
        self, message_id: str, text: str,
        buttons: list[list[dict]] | None = None,
    ) -> bool:
        body: dict = {"text": _trim(text, MESSAGE_LIMIT), "format": "html"}
        if buttons is not None:
            body["attachments"] = [keyboard(buttons)] if buttons else []
        try:
            self._request("PUT", "/messages", params={"message_id": message_id},
                          payload=body)
            return True
        except MaxApiError as exc:
            logger.warning("Не удалось отредактировать сообщение %s: %s", message_id, exc)
            return False

    def answer_callback(
        self, callback_id: str, *, notification: str | None = None,
        text: str | None = None, buttons: list[list[dict]] | None = None,
    ) -> bool:
        """
        Ответ на нажатие кнопки: всплывающее уведомление и/или замена
        текста исходного сообщения.
        """
        body: dict = {}
        if notification:
            body["notification"] = notification[:200]
        if text is not None:
            message: dict = {"text": _trim(text, MESSAGE_LIMIT), "format": "html"}
            if buttons is not None:
                message["attachments"] = [keyboard(buttons)] if buttons else []
            body["message"] = message

        try:
            self._request("POST", "/answers",
                          params={"callback_id": callback_id}, payload=body)
            return True
        except MaxApiError as exc:
            logger.warning("Не удалось ответить на callback: %s", exc)
            return False

    # -- изображения --------------------------------------------------------

    def upload_image(self, image_path: str | Path) -> dict | None:
        """
        Двухшаговая загрузка: получить временный URL, залить туда файл.
        Возвращает готовое вложение или None — тогда пост уйдёт текстом.
        """
        path = Path(image_path)
        if not path.is_file():
            logger.warning("Файл изображения не найден: %s", path)
            return None

        try:
            slot = self._request("POST", "/uploads", params={"type": "image"})
            upload_url = slot.get("url")
            if not upload_url:
                logger.warning("MAX не вернул url для загрузки")
                return None

            with open(path, "rb") as handle:
                response = self.session.post(
                    upload_url, files={"data": (path.name, handle, "image/jpeg")},
                    timeout=120,
                )
            if response.status_code != 200:
                logger.warning("Загрузка изображения: HTTP %s", response.status_code)
                return None

            data = response.json()
        except (MaxApiError, requests.RequestException, ValueError, OSError) as exc:
            logger.warning("Не удалось загрузить изображение в MAX: %s", exc)
            return None

        # Формат ответа зависит от версии API: либо словарь photos,
        # либо одиночный token. Поддерживаем оба.
        if isinstance(data, dict):
            if data.get("photos"):
                return {"type": "image", "payload": {"photos": data["photos"]}}
            token = data.get("token") or _dig(data, "token")
            if token:
                return {"type": "image", "payload": {"token": token}}

        logger.warning("Непонятный ответ загрузки: %s", str(data)[:200])
        return None

    # -- события ------------------------------------------------------------

    def get_updates(self, timeout: int = 30, limit: int = 100) -> list[MaxUpdate]:
        """
        Длинный опрос. Маркер запоминается внутри клиента, поэтому одно
        и то же событие не придёт дважды.
        """
        params: dict = {"limit": limit, "timeout": timeout}
        if self._marker is not None:
            params["marker"] = self._marker

        try:
            data = self._request(
                "GET", "/updates", params=params,
                timeout=timeout + 15, retries=2,
            )
        except MaxApiError as exc:
            logger.warning("Опрос событий не удался: %s", exc)
            time.sleep(3)
            return []

        self._marker = data.get("marker", self._marker)
        updates = data.get("updates") or []

        if updates and logger.isEnabledFor(10):  # DEBUG
            logger.debug("Сырые события: %s", json.dumps(updates, ensure_ascii=False)[:2000])

        return [parse_update(item) for item in updates]


# --------------------------------------------------------------------------

def _trim(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strip_tags(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "")
