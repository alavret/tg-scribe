import asyncio
import os
import sys
import urllib.request
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from telethon.tl.types import PeerUser, User

from tgc.client import proxy_spec, resolve, resolve_proxy, system_proxy

ANNA = User(id=1, first_name="Анна")
ANNA_S = User(id=2, first_name="Анна", last_name="Смирнова")
PETR = User(id=3, first_name="Пётр")


class FakeClient:
    def __init__(self, found=()):
        self.found = found

    async def get_me(self):
        return "self"

    async def __call__(self, request):  # contacts.SearchRequest
        return SimpleNamespace(users=list(self.found), chats=[], my_results=[PeerUser(u.id) for u in self.found])

    async def get_dialogs(self):
        return []


def test_me_is_saved_messages():
    assert asyncio.run(resolve(FakeClient(), "Me")) == "self"


def test_resolve_by_name():
    assert asyncio.run(resolve(FakeClient([ANNA, ANNA_S]), "анна")) is ANNA  # exact name wins
    assert asyncio.run(resolve(FakeClient([ANNA_S]), "Анна")) is ANNA_S  # one partial match is taken
    with pytest.raises(ToolError, match="несколько"):
        asyncio.run(resolve(FakeClient([ANNA_S, PETR]), "р"))
    with pytest.raises(ToolError, match="не найден"):
        asyncio.run(resolve(FakeClient(), "Анна"))


def test_proxy_spec_parses_url():
    assert proxy_spec("socks5://127.0.0.1:12334") == {
        "proxy_type": "socks5", "addr": "127.0.0.1", "port": 12334, "rdns": True,
    }


def test_proxy_spec_with_credentials():
    spec = proxy_spec("http://user:pass@127.0.0.1:8080")
    assert spec["proxy_type"] == "http" and spec["username"] == "user" and spec["password"] == "pass"


def test_proxy_spec_rejects_unknown_scheme():
    with pytest.raises(ToolError, match="ожидается"):
        proxy_spec("ftp://127.0.0.1:21")


def test_proxy_spec_none_when_unset():
    assert proxy_spec(None) is None


def test_proxy_spec_socks_means_socks5():
    assert proxy_spec("socks://127.0.0.1:1080")["proxy_type"] == "socks5"


def test_resolve_proxy_explicit_config_wins(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxy", lambda: "http://system:1")
    assert resolve_proxy("socks5://config:2")["addr"] == "config"


def test_resolve_proxy_direct_disables_system_proxy(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxy", lambda: "http://system:1")
    assert resolve_proxy("direct") is None
    assert resolve_proxy("none") is None


def test_resolve_proxy_falls_back_to_system(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxy", lambda: "http://system:1")
    assert resolve_proxy(None)["addr"] == "system"


def test_system_proxy_env_vars_first(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies_environment",
                        lambda: {"no": "localhost", "https": "http://env:3"})
    assert system_proxy() == "http://env:3"


def test_system_proxy_registry_checked_despite_no_proxy(monkeypatch):
    # только no_proxy в окружении: getproxies() на этом останавливается, system_proxy — нет
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"no": "localhost"})
    monkeypatch.setattr(urllib.request, "getproxies_registry",
                        lambda: {"http": "http://reg:4", "https": "http://reg:4"}, raising=False)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "platform", "win32")
    assert system_proxy() == "http://reg:4"


def test_system_proxy_macos_sysconf(monkeypatch):
    # macOS: системный прокси из System Configuration (Network → Proxies)
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf",
                        lambda: {"https": "http://mac:6152"}, raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert system_proxy() == "http://mac:6152"


def test_system_proxy_macos_socks_only(monkeypatch):
    # macOS: включён только SOCKS-прокси — _scproxy отдаёт его как http://host:port
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf",
                        lambda: {"socks": "http://127.0.0.1:1080"}, raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert system_proxy() == "socks5://127.0.0.1:1080"


def test_system_proxy_none_when_nowhere(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    assert system_proxy() is None


def test_parallel_calls_queue_instead_of_busy(tmp_path, monkeypatch):
    """Agents run read-only tools in parallel; in one process they must wait, not fail on the file lock."""
    import tgc.client as client

    class Client:
        def __init__(self, *a, **kw): ...
        async def connect(self): ...
        async def is_user_authorized(self): return True
        async def disconnect(self): ...

    monkeypatch.setattr(client, "CONF_DIR", tmp_path)
    monkeypatch.setattr(client, "load_config", lambda: (1, "h", "direct"))
    monkeypatch.setattr(client, "TelegramClient", Client)

    async def call():
        async with client.telegram():
            await asyncio.sleep(0.01)

    async def both():
        await asyncio.gather(call(), call())

    asyncio.run(both())
