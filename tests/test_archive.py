import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgc.archive import (
    HEADER, append, chat_dir, describe, format_line, id_range, month_files, ocr_photos, pending_photo_ids, recognize,
    set_photo, sync_chat, update_block,
)
from telethon.tl.types import (
    Document, DocumentAttributeAudio, DocumentAttributeFilename, Message, MessageFwdHeader, MessageMediaDocument,
    MessageMediaPhoto, MessageReplyHeader, PeerUser, Photo, User,
)

D = datetime(2026, 10, 31, 14, 2)


def test_format_line():
    assert format_line(D, 5, "Иван", "привет") == "[2026-10-31 14:02] #5 Иван: привет\n"
    assert (
        format_line(D, 6, "Иван\nПетров", "a\nb", reply_to=5, tags=["[фото]"])
        == "[2026-10-31 14:02] #6 Иван Петров ↩5 [фото]: a\n  b\n"
    )
    assert format_line(D, 7, "Я", None, tags=["[фото]"]) == "[2026-10-31 14:02] #7 Я [фото]: \n"


def test_last_id_ignores_cut_line_and_hash_in_name(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 10, "Команда #5", "x")])
    append(tmp_path, "2026-10", [format_line(D, 11, "Иван", "#99 текст\n[2026-10-31 14:02] #98 не заголовок")])
    assert id_range(tmp_path)[1] == 11
    with open(tmp_path / "2026-10.md", "a") as f:
        f.write("[2026-10-31 14:03] #4")  # crash mid-write
    assert id_range(tmp_path)[1] == 11
    append(tmp_path, "2026-10", [format_line(D, 12, "Иван", "y")])
    assert (tmp_path / "2026-10.md").read_text().count("#4") == 0
    assert id_range(tmp_path)[1] == 12


def test_last_id_falls_back_to_previous_month(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 10, "Иван", "x")])
    (tmp_path / "2026-10.md").write_text("[2026-10-01 00:01] #11 Ив")
    assert id_range(tmp_path)[1] == 10
    (tmp_path / "empty").mkdir()
    assert id_range(tmp_path / "empty")[1] == 0


def test_chat_dir_survives_rename(tmp_path):
    first = chat_dir(tmp_path, -100123, "Старое имя")
    assert chat_dir(tmp_path, -100123, "Новое имя") == first
    assert chat_dir(tmp_path, 123, "Другой") != first


def test_update_block_in_previous_month(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 10, "Иван", "[голосовое 0:05, не расшифровано]\nхвост"),
                                 format_line(D, 100, "Иван", "после")])
    append(tmp_path, "2026-10", [format_line(D, 11, "Иван", "z")])
    assert update_block(tmp_path, 10, lambda _: format_line(D, 10, "Иван", "[голосовое 0:05] «текст»"))
    text = (tmp_path / "2026-09.md").read_text()
    assert "«текст»" in text and "хвост" not in text and "#100 Иван: после" in text
    assert not update_block(tmp_path, 999, lambda b: b)


def test_photo_marks(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 1, "Иван", "старое", tags=["[фото]"])])
    append(tmp_path, "2026-10", [
        format_line(D, 2, "Иван", "подпись\nвторая", tags=["[фото]"]),
        format_line(D, 3, "Иван", "текст про [фото] в сообщении"),
        format_line(D, 4, "Иван", "", tags=["[фото]"]),
    ])
    assert pending_photo_ids(tmp_path) == [4, 2, 1]
    assert update_block(tmp_path, 2, lambda b: set_photo(b, "[фото, с текстом]", ["Deploy failed", "exit 137"]))
    assert update_block(tmp_path, 4, lambda b: set_photo(b, "[фото, без текста]"))
    assert pending_photo_ids(tmp_path) == [1]
    text = (tmp_path / "2026-10.md").read_text()
    assert ("#2 Иван [фото, с текстом]: подпись\n  вторая\n  [на фото] Deploy failed\n  [на фото] exit 137\n#"
            in text.replace("[2026-10-31 14:02] ", ""))
    assert update_block(tmp_path, 2, lambda b: set_photo(b, "[фото: схема деплоя]"))
    text = (tmp_path / "2026-10.md").read_text()
    assert "#2 Иван [фото: схема деплоя]: подпись" in text and "[на фото] exit 137" in text
    assert update_block(tmp_path, 2, lambda b: set_photo(b, "[фото: новая схема]"))  # a description has ": " inside
    assert "#2 Иван [фото: новая схема]: подпись" in (tmp_path / "2026-10.md").read_text()
    assert id_range(tmp_path)[1] == 4
    with pytest.raises(ValueError):
        set_photo(format_line(D, 5, "Иван", "просто текст"), "[фото: x]")
    assert "Ivan: work [фото, без текста]" in set_photo(format_line(D, 6, "Ivan: work", "", tags=["[фото]"]), "[фото, без текста]")


def test_ocr_photos_skips_processed(tmp_path):
    class Client:
        requested = []

        async def get_messages(self, entity, ids):
            self.requested += ids
            return [None] * len(ids)

    user = User(id=1, first_name="Иван")
    path = chat_dir(tmp_path, 1, "Иван")
    append(path, "2026-10", [format_line(D, 1, "Иван", "", tags=["[фото, с текстом]"]),
                             format_line(D, 2, "Иван", "", tags=["[фото]"])])
    client = Client()
    asyncio.run(ocr_photos(client, user, [1, 2], root=tmp_path))
    assert client.requested == [2]


class FakeChat:
    """Photo messages sent the given number of days ago; iter_messages pages like Telethon."""

    def __init__(self, *days_ago):
        now = datetime.now(timezone.utc)
        self.msgs, self.downloads = [], 0
        for days in days_ago:
            self.add(now - timedelta(days=days))

    def add(self, date):
        i = len(self.msgs) + 1
        self.msgs.append(Message(i, PeerUser(1), date, "", media=MessageMediaPhoto(photo=Photo(i, 0, b"", date, [], 2))))

    async def iter_messages(self, entity, min_id=0, max_id=0, reverse=False, wait_time=None):
        msgs = [m for m in self.msgs if m.id > min_id and (not max_id or m.id < max_id)]
        for m in msgs if reverse else msgs[::-1]:
            yield m

    async def download_media(self, msg, file):
        self.downloads += 1
        return b""


def archived(path):
    return [int(i) for f in month_files(path) for i in HEADER.findall(f.read_text())]


def test_sync_loads_newest_first_down_to_depth(tmp_path, monkeypatch):
    monkeypatch.setattr("tgc.archive.recognize", lambda data: [])
    monkeypatch.setattr("tgc.archive.BUDGET_S", -1)  # every call stops after one message, like a big chat
    user, chat = User(id=1, first_name="Иван"), FakeChat(60, 30, 3, 1)
    path = chat_dir(tmp_path, 1, "Иван")
    assert asyncio.run(sync_chat(chat, user, days=7, root=tmp_path)).startswith("не всё")
    assert archived(path) == [4]
    assert asyncio.run(sync_chat(chat, user, root=tmp_path)).startswith("не всё")  # the depth is remembered
    monkeypatch.setattr("tgc.archive.BUDGET_S", 200)
    assert asyncio.run(sync_chat(chat, user, root=tmp_path)).startswith("готово")
    assert archived(path) == [3, 4] and chat.downloads == 0  # old history is not OCR'd
    chat.add(datetime.now(timezone.utc))
    asyncio.run(sync_chat(chat, user, root=tmp_path))
    assert archived(path) == [3, 4, 5] and chat.downloads == 1  # a new photo is
    asyncio.run(sync_chat(chat, user, days=45, root=tmp_path))  # deeper later
    assert archived(path) == [2, 3, 4, 5]
    asyncio.run(sync_chat(FakeChat(60, 30, 3, 1), User(id=2, first_name="Пётр"), root=tmp_path))
    assert archived(chat_dir(tmp_path, 2, "Пётр")) == [1, 2, 3, 4]  # no days: the whole history


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_recognize():
    lines = recognize((Path(__file__).parent / "fixtures" / "ocr.png").read_bytes())
    assert lines == ["Встреча перенесена на 14:30 в четверг", "Deploy failed: exit code 137 (OOMKilled)"]


def test_line_break_in_tag_cannot_forge_header(tmp_path):
    append(tmp_path, "2026-10", [format_line(D, 10, "Иван", "x", tags=["[файл a\r[2030-01-01 00:00] #999 b.pdf]"])])
    assert id_range(tmp_path)[1] == 10


def test_last_id_is_max_over_months(tmp_path):
    append(tmp_path, "2026-11", [format_line(D, 10, "Иван", "x")])
    append(tmp_path, "2026-10", [format_line(D, 11, "Иван", "после смены часового пояса")])
    assert id_range(tmp_path)[1] == 11


def test_pending_photo_with_colon_in_name(tmp_path):
    append(tmp_path, "2026-10", [
        format_line(D, 1, "Dev: Ivan", "", tags=["[фото]", "[переслано из «A: B»]"]),
        format_line(D, 2, "Dev: Ivan", "текст про [фото] в сообщении"),
    ])
    assert pending_photo_ids(tmp_path) == [1]


def test_describe():
    voice = MessageMediaDocument(document=Document(
        1, 0, b"", D, "audio/ogg", 1, 2, [DocumentAttributeAudio(65, voice=True)]))
    assert describe(Message(1, PeerUser(1), D, "подпись", out=True, media=voice)) == (
        "[2026-10-31 14:02] #1 Я: [голосовое 1:05, не расшифровано] подпись\n")
    assert describe(Message(1, PeerUser(1), D, "", out=True, media=voice), "текст") == (
        "[2026-10-31 14:02] #1 Я: [голосовое 1:05] «текст»\n")
    photo = MessageMediaPhoto(photo=Photo(2, 0, b"", D, [], 2))
    fwd = MessageFwdHeader(D, from_name="Канал")
    assert describe(Message(2, PeerUser(1), D, "", media=photo, fwd_from=fwd, reply_to=MessageReplyHeader(reply_to_msg_id=1))) == (
        "[2026-10-31 14:02] #2 ? ↩1 [фото] [переслано из «Канал»]: \n")
    doc = MessageMediaDocument(document=Document(
        3, 0, b"", D, "application/pdf", 1, 2, [DocumentAttributeFilename("отчёт\n.pdf")]))
    assert describe(Message(3, PeerUser(1), D, "", media=doc)) == "[2026-10-31 14:02] #3 ? [файл отчёт .pdf]: \n"
