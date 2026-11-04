import asyncio
import io
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgc.archive import (
    HEADER, Unreadable, append, chat_dir, describe, excerpt, extract_text, format_line, id_range, month_files, ocr_photos,
    pending_ids, read_files, recognize, set_media, sync_chat, update_block,
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
    assert pending_ids(tmp_path, "фото") == [4, 2, 1]
    assert update_block(tmp_path, 2, lambda b: set_media(b, ", с текстом", ["Deploy failed", "exit 137"]))
    assert update_block(tmp_path, 4, lambda b: set_media(b, ", без текста"))
    assert pending_ids(tmp_path, "фото") == [1]
    text = (tmp_path / "2026-10.md").read_text()
    assert ("#2 Иван [фото, с текстом]: подпись\n  вторая\n  [на фото] Deploy failed\n  [на фото] exit 137\n#"
            in text.replace("[2026-10-31 14:02] ", ""))
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": схема деплоя"))
    text = (tmp_path / "2026-10.md").read_text()
    assert "#2 Иван [фото: схема деплоя]: подпись" in text and "[на фото] exit 137" in text
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": новая схема"))  # a description has ": " inside
    assert "#2 Иван [фото: новая схема]: подпись" in (tmp_path / "2026-10.md").read_text()
    assert id_range(tmp_path)[1] == 4
    with pytest.raises(ValueError):
        set_media(format_line(D, 5, "Иван", "просто текст"), ": x")
    assert "Ivan: work [фото, без текста]" in set_media(format_line(D, 6, "Ivan: work", "", tags=["[фото]"]), ", без текста")


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
    chat.msgs.append(doc_msg(6, "план.txt", date=datetime.now(timezone.utc)))
    asyncio.run(sync_chat(chat, user, root=tmp_path))
    assert chat.downloads == 2  # and a new file
    assert "[файл план.txt, 1 КБ, без текста]" in (path / f"{datetime.now():%Y-%m}.md").read_text()
    asyncio.run(sync_chat(chat, user, days=45, root=tmp_path))  # deeper later
    assert archived(path) == [2, 3, 4, 5, 6]
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
    assert pending_ids(tmp_path, "фото") == [1]


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
    assert describe(Message(3, PeerUser(1), D, "", media=doc)) == "[2026-10-31 14:02] #3 ? [файл отчёт .pdf, 1 КБ]: \n"
    odd = doc_msg(4, "Итоги: план].txt", size=2_345_678)
    assert describe(odd) == "[2026-10-31 14:02] #4 ? [файл Итоги - план).txt, 2.3 МБ]: \n"


def doc_msg(i, name, size=10, date=D):
    media = MessageMediaDocument(document=Document(i, 0, b"", date, "application/octet-stream", size, 2,
                                                   [DocumentAttributeFilename(name)]))
    return Message(i, PeerUser(1), date, "", media=media)


def test_file_marks(tmp_path):
    photo = MessageMediaPhoto(photo=Photo(5, 0, b"", D, [], 2))
    append(tmp_path, "2026-10", [
        describe(doc_msg(1, "смета.PDF")),
        describe(doc_msg(2, "архив.zip")),  # not readable locally: never pending
        format_line(D, 3, "Иван", "", tags=["[файл старый.docx]"]),  # archived before sizes were added
        describe(doc_msg(4, "план.md")),
        describe(Message(5, PeerUser(1), D, "", media=photo)),
    ])
    assert pending_ids(tmp_path, "файл") == [4, 3, 1] and pending_ids(tmp_path, "фото") == [5]
    assert update_block(tmp_path, 1, lambda b: set_media(b, ", с текстом", ["Итого: 120 000 ₽"]))
    assert update_block(tmp_path, 4, lambda b: set_media(b, ", слишком большой"))
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": фото с объекта"))
    assert pending_ids(tmp_path, "файл") == [3]
    text = (tmp_path / "2026-10.md").read_text().replace("[2026-10-31 14:02] ", "")
    assert "#1 ? [файл смета.PDF, 1 КБ, с текстом]: \n  [в файле] Итого: 120 000 ₽\n" in text
    assert "#2 ? [файл архив.zip, 1 КБ: фото с объекта]: " in text
    assert "#4 ? [файл план.md, 1 КБ, слишком большой]: " in text
    assert update_block(tmp_path, 1, lambda b: set_media(b, ": смета на кухню"))
    assert "[файл смета.PDF, 1 КБ: смета на кухню]: \n  [в файле] Итого" in (tmp_path / "2026-10.md").read_text()


def test_extract_text():
    assert extract_text("a.txt", "﻿первая\n\n  вторая   строка ".encode()) == ["первая", "вторая строка"]
    assert extract_text("a.TXT", "привет".encode("cp1251")) == ["привет"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", (
            "<w:body><w:p><w:r><w:t>Смета</w:t></w:r><w:r><w:tab/><w:t xml:space=\"preserve\"> на &amp; </w:t></w:r>"
            "<w:r><w:t>кухню</w:t></w:r></w:p><w:p></w:p><w:p><w:r><w:t>Итого</w:t></w:r></w:p></w:body>"))
    assert extract_text("a.docx", buf.getvalue()) == ["Смета на & кухню", "Итого"]
    with pytest.raises(Unreadable):
        extract_text("a.docx", b"not a zip")
    assert excerpt(["abc", "defg", "h"], limit=5) == ["abc", "de … (дальше — fetch_media)"]
    assert excerpt(["abc", "de", "f"], limit=5) == ["abc", "de", "… (дальше — fetch_media)"]
    assert excerpt(["abc"], limit=5) == ["abc"]


def test_read_files(tmp_path):
    class Client:
        def __init__(self, msgs):
            self.msgs, self.downloads = msgs, 0

        async def get_messages(self, entity, ids):
            return [self.msgs.get(i) for i in ids]

        async def download_media(self, msg, file):
            self.downloads += 1
            return "строка 1\nстрока 2".encode()

    user = User(id=1, first_name="Иван")
    path = chat_dir(tmp_path, 1, "Иван")
    big = doc_msg(3, "big.txt", size=60_000_000)
    append(path, "2026-10", [describe(doc_msg(1, "a.txt")), describe(doc_msg(2, "gone.txt")), describe(big)])
    client = Client({1: doc_msg(1, "a.txt"), 3: big})
    assert "осталось необработанных файлов 0" in asyncio.run(read_files(client, user, root=tmp_path))
    assert client.downloads == 1  # the big file is not downloaded
    text = (path / "2026-10.md").read_text()
    assert "[файл a.txt, 1 КБ, с текстом]: \n  [в файле] строка 1\n  [в файле] строка 2\n" in text
    assert "[файл gone.txt, 1 КБ, недоступно]" in text and "[файл big.txt, 60.0 МБ, слишком большой]" in text
