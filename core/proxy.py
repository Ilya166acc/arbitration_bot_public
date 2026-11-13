"""Общие настройки прокси для Playwright и requests.

PROXY_URL хранится только в окружении/.env. В логи попадают адрес прокси и
причина ошибки без логина, пароля и полного URL с credentials.
"""
import logging
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


@dataclass(frozen=True)
class ProxySettings:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def server(self) -> str:
        # Playwright/Chromium понимает socks5, но не socks5h.
        scheme = "socks5" if self.scheme == "socks5h" else self.scheme
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}"

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        auth = ""
        if self.username is not None:
            auth = (
                f"{quote(self.username, safe='')}:"
                f"{quote(self.password or '', safe='')}@"
            )
        return f"{self.scheme}://{auth}{host}:{self.port}"

    def as_playwright(self) -> dict:
        proxy = {"server": self.server}
        if self.username is not None:
            proxy["username"] = self.username
            proxy["password"] = self.password or ""
        return proxy

    def as_requests(self) -> dict:
        return {"http": self.url, "https": self.url}


def parse_proxy_url(proxy_url: str | None) -> ProxySettings | None:
    """Разбирает PROXY_URL; при ошибке логирует её и возвращает None (direct)."""
    if not proxy_url or not proxy_url.strip():
        logger.error(
            "PROXY_URL не задан — подключаюсь к kad.arbitr.ru напрямую. "
            "Если сайт блокирует сервер, задайте прокси в .env."
        )
        return None

    try:
        parsed = urlsplit(proxy_url.strip())
        scheme = parsed.scheme.lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise ValueError(
                "поддерживаются только http://, https://, socks5:// и socks5h://"
            )

        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            # Не пробрасываем текст urllib: в некорректном URL fragment пароля
            # может оказаться на месте порта и попасть в лог.
            raise ValueError("port должен быть числом 1..65535") from None
        if not host or port is None:
            raise ValueError("URL должен содержать host и port")
        if not 1 <= port <= 65535:
            raise ValueError("port должен быть в диапазоне 1..65535")
        if any(character.isspace() for character in host):
            raise ValueError("host не должен содержать пробелы")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("URL прокси не должен содержать path, query или fragment")

        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
        if (username is None) != (password is None):
            raise ValueError("username и password должны быть заданы вместе")
        if username is not None and (not username or not password):
            raise ValueError("username и password не должны быть пустыми")

        return ProxySettings(
            scheme=scheme,
            host=host,
            port=port,
            username=username,
            password=password,
        )
    except (ValueError, UnicodeError) as exc:
        # Не логируем исходный URL: внутри могут быть логин и пароль.
        logger.error(
            "Некорректный PROXY_URL (%s) — подключаюсь напрямую. "
            "Ожидается http://USER:PASSWORD@HOST:PORT или socks5://USER:PASSWORD@HOST:PORT.",
            exc,
        )
        return None


def redact_proxy_error(error: object, proxy_url: str | None) -> str:
    """Убирает credentials из текста ошибки перед логом/исключением."""
    text = str(error)
    if not proxy_url:
        return text

    try:
        parsed = urlsplit(proxy_url)
    except ValueError:
        return text

    secrets = {proxy_url}
    for value in (parsed.username, parsed.password):
        if value:
            secrets.add(value)
            secrets.add(unquote(value))
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text
