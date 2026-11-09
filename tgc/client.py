import asyncio
import os
import sys
import tomllib
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.mcpserver.exceptions import ToolError
from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.contacts import SearchRequest
from telethon.utils import get_display_name, get_peer_id

CONF_DIR = Path.home() / ".tgc"
SESSION = CONF_DIR / "tgc"  # Telethon adds .session

if os.name == "nt":  # Windows: блокировка байта через msvcrt, на POSIX — flock
    import msvcrt

    def _lock_file(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl

    def _lock_file(f):
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

# Agents call read-only tools in parallel: inside one process they queue here
# instead of failing on the file lock, which is only for other processes.
_busy = asyncio.Lock()


def load_config():
    path = CONF_DIR / "config.toml"
    if not path.exists():
        raise ToolError(
            f"Нет {path}. Создай его с api_id и api_hash из https://my.telegram.org/apps:\n"
            'api_id = 12345\napi_hash = "abc..."'
        )
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    return int(conf["api_id"]), conf["api_hash"], conf.get("proxy")


def proxy_spec(url):
    """Строка вида 'socks5://127.0.0.1:12334' -> dict-прокси для Telethon; None, если прокси не задан."""
    if not url:
        return None
    p = urlparse(url)
    scheme = p.scheme.lower()
    if scheme == "socks":  # версия SOCKS не указана — считаем SOCKS5
        scheme = "socks5"
    if scheme not in ("socks5", "socks4", "http") or not p.hostname or not p.port:
        raise ToolError(
            f"proxy = {url!r}: ожидается socks5://хост:порт, socks4://хост:порт или http://хост:порт"
        )
    spec = {"proxy_type": scheme, "addr": p.hostname, "port": p.port, "rdns": True}
    if p.username:
        spec["username"] = p.username
    if p.password:
        spec["password"] = p.password
    return spec


def _os_proxies():
    """Системные настройки прокси ОС: реестр Windows или System Configuration macOS.
    {} — Linux, настройки недоступны или не заданы."""
    if sys.platform == "darwin":
        reader = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    elif os.name == "nt":
        reader = getattr(urllib.request, "getproxies_registry", None)
    else:
        reader = None
    if reader is None:
        return {}
    try:
        return reader() or {}
    except Exception:  # настройки ОС не читаются — подключаемся напрямую
        return {}


def system_proxy():
    """Прокси из настроек ОС: переменные окружения (http_proxy/https_proxy), затем системный прокси —
    реестр Windows (ProxyEnable/ProxyServer) или System Configuration macOS (Network → Proxies).

    urllib.request.getproxies() не подходит: если задана только no_proxy, он возвращает
    {'no': ...} — словарь истинен, и системные настройки не проверяются вовсе."""
    env = urllib.request.getproxies_environment()
    os_proxies = _os_proxies()
    for source in (env, os_proxies):
        for scheme in ("https", "http"):
            if source.get(scheme):
                return source[scheme]
    socks = os_proxies.get("socks")  # macOS: включён только SOCKS-прокси, без HTTP
    if socks:
        # SystemConfiguration не указывает версию SOCKS и отдаёт значение как http://host:port
        p = urlparse(socks)
        if p.hostname and p.port:
            return f"socks5://{p.hostname}:{p.port}"
    return None


def resolve_proxy(configured):
    """proxy из config.toml: URL — явно заданный прокси, 'direct'/'none' — без прокси,
    не задан — системный прокси ОС (см. system_proxy)."""
    if not configured:
        return proxy_spec(system_proxy())
    if configured.strip().lower() in ("direct", "none", "off"):
        return None
    return proxy_spec(configured)


@asynccontextmanager
async def telegram(login=False):
    """One Telegram connection per call: every agent session runs its own server process,
    and two processes on one SQLite session file fail with `database is locked`.
    login=True — подключение без проверки входа: так tgc.login сам ведёт QR-поток авторизации."""
    async with _busy:
        CONF_DIR.mkdir(mode=0o700, exist_ok=True)
        with open(CONF_DIR / "tgc.lock", "w") as lock:
            try:
                _lock_file(lock)
            except OSError:  # BlockingIOError (POSIX) / PermissionError (Windows): занято
                raise ToolError("Telegram занят другой сессией, повтори позже") from None
            api_id, api_hash, configured_proxy = load_config()
            # Longer FloodWaits are raised, so a call never sleeps past its time budget.
            client = TelegramClient(
                str(SESSION), api_id, api_hash, flood_sleep_threshold=30,
                proxy=resolve_proxy(configured_proxy),
            )
            try:
                await client.connect()
                if not login and not await client.is_user_authorized():
                    raise ToolError("Нет входа в Telegram: выполни `uv run python -m tgc.login`")
                yield client
            except (RPCError, ConnectionError) as e:  # MCP hides the text of other exceptions from Claude
                raise ToolError(f"Telegram: {e}") from e
            finally:
                await client.disconnect()


async def resolve(client, chat: str):
    """Chat by marked id, @username, part of its title, or «me» for Saved Messages."""
    if chat.lower() == "me":
        return await client.get_me()
    if chat.lstrip("-").isdigit() or chat.startswith("@"):
        key = int(chat) if chat.lstrip("-").isdigit() else chat
        try:
            return await client.get_entity(key)
        except ValueError:
            await client.get_dialogs()  # fills the entity cache, needed for bare ids
            try:
                return await client.get_entity(key)
            except ValueError:
                raise ToolError(f"Чат «{chat}» не найден") from None
    found = await search(client, chat)
    if not found:  # search misses some non-contact chats; the full scan is slow (~1 min on 5k chats)
        found = [d.entity for d in await client.get_dialogs() if chat.lower() in d.name.lower()]
    matches = [e for e in found if get_display_name(e).lower() == chat.lower()] or found
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError(f"Чат «{chat}» не найден")
    rows = "\n".join(f"{get_peer_id(e)}\t{get_display_name(e)}" for e in matches[:20])
    raise ToolError(f"Под «{chat}» подходит несколько чатов, укажи id:\n{rows}")


async def search(client, query: str):
    """Server-side name search among the user's own chats: one request instead of paging all dialogs."""
    r = await client(SearchRequest(q=query, limit=50))
    by_id = {get_peer_id(e): e for e in r.users + r.chats}
    return [by_id[get_peer_id(p)] for p in r.my_results if get_peer_id(p) in by_id]
