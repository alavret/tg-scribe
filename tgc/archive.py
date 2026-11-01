import asyncio
import re
import time
from pathlib import Path

from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import TranscribeAudioRequest
from telethon.tl.types import MessageService
from telethon.utils import get_display_name, get_peer_id

ARCHIVE = Path(__file__).resolve().parent.parent / "archive"
BUDGET_S = 200  # MCP calls time out; a big chat is synced over several calls
MAX_AUTO_VOICE = 20
MAX_AUTO_PHOTO = 20
FLUSH_EVERY = 200
HEADER = re.compile(r"^\[[^\]]+\] #(\d+) ", re.M)


# --- pure archive format -------------------------------------------------

def format_line(date, msg_id, sender, text, reply_to=None, tags=()):
    head = f"[{date:%Y-%m-%d %H:%M}] #{msg_id} {' '.join(sender.split())}"
    if reply_to:
        head += f" ↩{reply_to}"
    if tags:  # tags carry sender-chosen text (file names); a line break in it would forge a header
        head += " " + " ".join(" ".join(t.split()) for t in tags)
    lines = (text or "").splitlines() or [""]
    return f"{head}: {lines[0]}" + "".join(f"\n  {line}" for line in lines[1:]) + "\n"


def chat_dir(root: Path, chat_id: int, title: str) -> Path:
    """Found by id, so a renamed chat keeps its folder."""
    found = sorted(root.glob(f"*__{chat_id}"))
    if found:
        return found[0]
    path = root / f"{re.sub(r'[^\w.-]+', '-', title).strip('-')[:60] or 'chat'}__{chat_id}"
    path.mkdir(parents=True)
    return path


def month_files(path: Path):
    return sorted(path.glob("????-??.md"))


def id_range(path: Path) -> tuple[int, int]:
    """(oldest, newest) archived id, (0, 0) if none. Over all months: after a time zone change
    or a history import, the newest id can sit in an older file."""
    ids = []
    for f in month_files(path):
        text = f.read_text()
        ids += map(int, HEADER.findall(text[: text.rfind("\n") + 1]))  # a line cut by a crash has no "\n"
    return (min(ids), max(ids)) if ids else (0, 0)


def append(path: Path, month: str, lines: list[str]):
    f = path / f"{month}.md"
    if f.exists():
        text = f.read_text()
        if text and not text.endswith("\n"):  # drop a line cut by a crash before appending
            f.write_text(text[: text.rfind("\n") + 1])
    with open(f, "a") as out:
        out.write("".join(lines))


def prepend(path: Path, month: str, lines: list[str]):
    f = path / f"{month}.md"
    tmp = f.with_suffix(".tmp")
    tmp.write_text("".join(lines) + (f.read_text() if f.exists() else ""))
    tmp.replace(f)


def update_block(path: Path, msg_id: int, fn) -> bool:
    """Rewrites one message (header plus continuation lines) with fn(block)."""
    block = re.compile(rf"^\[[^\]]+\] #{msg_id} .*\n(?:  .*\n)*", re.M)
    for f in reversed(month_files(path)):
        text = f.read_text()
        m = block.search(text)
        if m:
            tmp = f.with_suffix(".tmp")
            tmp.write_text(text[: m.start()] + fn(m.group()) + text[m.end():])
            tmp.replace(f)
            return True
    return False


PHOTO_TAG = re.compile(r"\[фото[^\]]*\]")


def set_photo(block: str, tag: str, lines=()) -> str:
    head, _, rest = block.partition("\n")
    if not PHOTO_TAG.search(head):
        raise ValueError("not a photo")
    # Tags come before the text, so the first match is the tag even if the text says "[фото]".
    return PHOTO_TAG.sub(lambda _: tag, head, count=1) + "\n" + rest + "".join(f"  [на фото] {s}\n" for s in lines)


def pending_photo_ids(path: Path) -> list[int]:
    """Photos not yet OCR'd, newest first."""
    ids = []
    for f in reversed(month_files(path)):
        # [фото] followed only by other tags and then ": ", so a sender name may contain ": " too.
        ids += reversed([int(i) for i in re.findall(r"^\[[^\]]+\] #(\d+) .*?\[фото\](?: \[[^\]]*\])*: ", f.read_text(), re.M)])
    return ids


class Unreadable(Exception):
    pass


def recognize(data: bytes) -> list[str]:
    """Text on an image via Apple Vision (macOS only)."""
    import Vision
    from Foundation import NSData

    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["ru-RU", "en-US"])
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(data, len(data)), None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise Unreadable(f"Vision: {err}")
    return [s for r in req.results() if (s := " ".join(r.topCandidates_(1)[0].string().split()))]


# --- Telegram messages ---------------------------------------------------

def is_voice(msg):
    return bool(msg.voice or msg.video_note)


def describe(msg, transcript=None):
    tags, text = [], msg.message or ""
    if is_voice(msg):
        dur = int(msg.file.duration or 0)
        label = f"{'голосовое' if msg.voice else 'кружок'} {dur // 60}:{dur % 60:02}"
        voice = f"[{label}] «{transcript}»" if transcript is not None else f"[{label}, не расшифровано]"
        text = f"{voice} {text}".strip()
    elif msg.photo:
        tags.append("[фото]")
    elif msg.sticker:
        tags.append(f"[стикер {msg.file.emoji or ''}]".replace(" ]", "]"))
    elif msg.gif:
        tags.append("[gif]")
    elif msg.video:
        tags.append("[видео]")
    elif msg.poll:
        tags.append("[опрос]")
    elif msg.geo:
        tags.append("[геопозиция]")
    elif msg.contact:
        tags.append("[контакт]")
    elif msg.document:
        tags.append(f"[файл {msg.file.name or ''}]".replace(" ]", "]"))
    if msg.fwd_from:
        src = msg.forward and (msg.forward.chat or msg.forward.sender)
        tags.append(f"[переслано из «{get_display_name(src) if src else msg.fwd_from.from_name or '?'}»]")
    sender = "Я" if msg.out or getattr(msg.sender, "is_self", False) else get_display_name(msg.sender) or msg.post_author or "?"
    return format_line(msg.date.astimezone(), msg.id, sender, text, msg.reply_to_msg_id, tags)


async def transcribe_one(client, peer, msg_id):
    """Telegram Premium transcription; None if it is still not ready."""
    for _ in range(5):
        r = await client(TranscribeAudioRequest(peer, msg_id))
        if not r.pending:
            return r.text
        await asyncio.sleep(2)
    return None


async def ocr_photo(client, msg):
    """(tag, lines) for a photo; downloaded to memory only."""
    try:
        lines = await asyncio.to_thread(recognize, await client.download_media(msg, file=bytes))
    except Unreadable:  # broken file on Telegram's side; retrying won't help
        return "[фото, не читается]", []
    return ("[фото, с текстом]", lines) if lines else ("[фото, без текста]", [])


async def ocr_photos(client, entity, ids=None, root=ARCHIVE):
    start = time.monotonic()
    path = chat_dir(root, get_peer_id(entity), get_display_name(entity))
    pending = pending_photo_ids(path)
    # Already processed photos are skipped: OCR again would duplicate lines and drop a description.
    ids = [i for i in ids if i in pending] if ids else pending
    done = failed = 0
    status = "готово"
    try:
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            for msg_id, msg in zip(batch, await client.get_messages(entity, ids=batch)):
                if time.monotonic() - start > BUDGET_S:
                    raise TimeoutError
                if not msg:
                    update_block(path, msg_id, lambda b: set_photo(b, "[фото, недоступно]"))
                    continue
                if not msg.photo:
                    continue
                try:
                    tag, lines = await ocr_photo(client, msg)
                except FloodWaitError:
                    raise
                except Exception:  # left as [фото], retried on the next call
                    failed += 1
                    continue
                update_block(path, msg_id, lambda b: set_photo(b, tag, lines))
                done += 1
    except TimeoutError:
        status = "не всё, вызови ocr_photos ещё раз"
    except FloodWaitError as e:
        status = f"Telegram просит подождать {e.seconds} с, потом вызови ocr_photos ещё раз"
    left = len(pending_photo_ids(path))
    return f"{status}: распознано {done}, ошибок {failed}, осталось необработанных фото {left}. Папка: {path}"


async def sync_chat(client, entity, days=None, root=ARCHIVE):
    """New messages first, then older history down to the target depth, newest first:
    an interrupted load leaves a gap-free archive that ends at the present."""
    start = time.monotonic()
    path = chat_dir(root, get_peer_id(entity), get_display_name(entity))
    first_id, last_id = id_range(path)
    # The depth (unix time, 0 = the whole history) is kept on disk: a big load spans several calls.
    depth = path / ".depth"
    target = float(depth.read_text()) if depth.exists() else None
    if days:
        since = time.time() - days * 86400
        target = since if target is None else min(target, since)
    elif target is None and last_id == 0:
        target = 0.0
    if target is not None:
        depth.write_text(str(target))
    buf: dict[str, list[str]] = {}
    new = old = voiced = unvoiced = photos = 0
    status = "готово"

    def check_budget():
        if time.monotonic() - start > BUDGET_S:
            raise TimeoutError

    def flush(write, newest_first=False):
        for month in sorted(buf, reverse=newest_first):
            write(path, month, buf[month][::-1] if newest_first else buf[month])
        buf.clear()

    try:
        if last_id:
            try:
                async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, wait_time=1):
                    if isinstance(msg, MessageService):
                        continue
                    transcript = None
                    if is_voice(msg):
                        if voiced < MAX_AUTO_VOICE and time.monotonic() - start < BUDGET_S:
                            try:
                                transcript = await transcribe_one(client, entity, msg.id)
                                await asyncio.sleep(1)
                            except FloodWaitError:
                                raise
                            except RPCError:
                                pass
                        voiced += transcript is not None
                        unvoiced += transcript is None
                    line = describe(msg, transcript)
                    if msg.photo and photos < MAX_AUTO_PHOTO and time.monotonic() - start < BUDGET_S:
                        try:
                            line = set_photo(line, *await ocr_photo(client, msg))
                            photos += 1
                        except FloodWaitError:
                            raise
                        except Exception:  # left as [фото] for ocr_photos
                            pass
                    buf.setdefault(f"{msg.date.astimezone():%Y-%m}", []).append(line)
                    new += 1
                    if new % FLUSH_EVERY == 0:
                        flush(append)
                    check_budget()
            finally:
                flush(append)
        if target is not None:
            # Old history is not transcribed or OCR'd: that is done lazily, when a task needs it.
            try:
                async for msg in client.iter_messages(entity, max_id=first_id, wait_time=1):
                    if msg.date.timestamp() < target:
                        break
                    if isinstance(msg, MessageService):
                        continue
                    buf.setdefault(f"{msg.date.astimezone():%Y-%m}", []).append(describe(msg))
                    old += 1
                    if old % FLUSH_EVERY == 0:
                        flush(prepend, newest_first=True)  # the month next to the archive goes first
                    check_budget()
            finally:
                flush(prepend, newest_first=True)
            depth.unlink()
    except TimeoutError:
        status = "не всё загружено, вызови sync_chat ещё раз"
    except FloodWaitError as e:
        status = f"Telegram просит подождать {e.seconds} с, потом вызови sync_chat ещё раз"

    voice = f", голосовых расшифровано {voiced}, не расшифровано {unvoiced}" if voiced or unvoiced else ""
    ocr = f", фото распознано {photos}" if photos else ""
    older = f", старых {old}" if old else ""
    return f"{status}: новых сообщений {new}{older}{voice}{ocr}. Папка: {path}"
