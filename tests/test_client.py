import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from telethon.tl.types import PeerUser, User

from tgc.client import resolve

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
