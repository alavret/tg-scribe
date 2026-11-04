import asyncio
from contextlib import asynccontextmanager

from telethon.tl.types import Document, DocumentAttributeFilename, Message, MessageMediaDocument, PeerUser, User

import tgc.server as server

USER = User(id=1, first_name="Иван")


def doc(i, size):
    media = MessageMediaDocument(document=Document(i, 0, b"", None, "application/pdf", size, 2,
                                                   [DocumentAttributeFilename(f"{i}.pdf")]))
    return Message(i, PeerUser(1), None, "", media=media)


def test_fetch_media_refuses_over_50_mb(tmp_path, monkeypatch):
    """fetch_media needs no confirmation, so it must never download a big file: that is fetch_large_file, behind ask."""
    downloads = []

    class Client:
        async def get_messages(self, entity, ids):
            return [doc(1, 60_000_000), doc(2, 1000)]

        async def download_media(self, msg, file):
            downloads.append(msg.id)
            return file

    @asynccontextmanager
    async def telegram():
        yield Client()

    async def resolve(client, chat):
        return USER

    monkeypatch.setattr(server, "telegram", telegram)
    monkeypatch.setattr(server, "resolve", resolve)
    monkeypatch.setattr(server, "ARCHIVE", tmp_path)
    out = asyncio.run(server.fetch_media("Иван", [1, 2]))
    assert downloads == [2]
    assert "#1: 60.0 МБ, больше 50 МБ, не скачан" in out
    assert f"#2: {tmp_path / 'Иван__1' / 'media' / '2_2.pdf'}" in out  # the original name is kept
