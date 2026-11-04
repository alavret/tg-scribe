import asyncio
import html
import io
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import TranscribeAudioRequest
from telethon.tl.types import MessageService
from telethon.utils import get_display_name, get_peer_id

ARCHIVE = Path(__file__).resolve().parent.parent / "archive"
BUDGET_S = 200  # MCP calls time out; a big chat is synced over several calls
MAX_AUTO_VOICE = 20
MAX_AUTO_MEDIA = 20  # photos and files each
MAX_FILE = 50_000_000
READABLE = {"txt", "md", "csv", "pdf", "docx", "doc", "rtf", "jpg", "jpeg", "png", "heic"}
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


MEDIA_TAG = re.compile(r"\[(фото|файл)(?=[ ,:\]])[^\]]*\]")
STATE = re.compile(r"(?:, (?:с текстом|без текста|недоступно|не читается|слишком большой)|: .*)?\]$")
PENDING = {"фото": r"\[фото\]", "файл": rf"\[файл [^\]]*\.(?i:{'|'.join(READABLE)})(?:, [\d.]+ [КМ]Б)?\]"}


def set_media(block: str, state: str, lines=()) -> str:
    """Sets the photo or file tag state (", с текстом", ": описание"…) and adds text lines below it."""
    head, _, rest = block.partition("\n")
    # Tags come before the text, so the first match is the tag even if the text says "[фото]".
    m = MEDIA_TAG.search(head)
    if not m:
        raise ValueError("not a photo or file")
    tag = STATE.sub("", m.group()) + state + "]"
    return head[: m.start()] + tag + head[m.end():] + "\n" + rest + "".join(f"  [на фото] {s}\n" for s in lines)


def pending_ids(path: Path, kind: str) -> list[int]:
    """Photos or readable files not yet processed, newest first."""
    ids = []
    for f in reversed(month_files(path)):
        # The tag followed only by other tags and then ": ", so a sender name may contain ": " too.
        ids += reversed([int(i) for i in re.findall(rf"^\[[^\]]+\] #(\d+) .*?{PENDING[kind]}(?: \[[^\]]*\])*: ", f.read_text(), re.M)])
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


def pdf_text(data: bytes) -> tuple[str, int]:
    """(text layer, page count) of a PDF via PDFKit (macOS only); a scan has no text."""
    from Foundation import NSData
    from Quartz import PDFDocument

    doc = PDFDocument.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
    if doc is None or doc.isLocked():
        raise Unreadable("PDF")
    return doc.string() or "", doc.pageCount()


def textutil(data: bytes, ext: str) -> str:
    """doc and rtf via macOS textutil; it reads files only, so the data goes to a temp file."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in.{ext}"
        src.write_bytes(data)
        r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(src)], capture_output=True, timeout=60)
    if r.returncode:
        raise Unreadable(f"textutil: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace")


def extract_text(name: str, data: bytes) -> tuple[list[str], str]:
    """(non-empty lines of a file's text, read locally; extra tag info such as ", 12 стр.")."""
    ext, info = Path(name).suffix.lower()[1:], ""
    if ext in ("jpg", "jpeg", "png", "heic"):
        return recognize(data), info
    if ext == "pdf":
        text, pages = pdf_text(data)
        info = f", {pages} стр."
    elif ext in ("doc", "rtf"):
        text = textutil(data, ext)
    elif ext == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z, z.open("word/document.xml") as f:
                xml = f.read(20_000_000).decode("utf-8", "replace")  # a capped read: a zip bomb stays small
        except (zipfile.BadZipFile, KeyError) as e:
            raise Unreadable(f"docx: {e}") from e
        text = "\n".join(html.unescape("".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p))) for p in xml.split("</w:p>"))
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251", "replace")
    return [s for line in text.splitlines() if (s := " ".join(line.split()))], info


def size_str(n: int) -> str:
    return f"{n / 1e6:.1f} МБ" if n >= 1e6 else f"{max(1, round(n / 1e3))} КБ"


# --- Telegram messages ---------------------------------------------------

def is_voice(msg):
    return bool(msg.voice or msg.video_note)


def is_file(msg):
    """A document that is not a sticker, gif, video or audio."""
    return bool(msg.document and not (msg.sticker or msg.gif or msg.video or msg.audio or is_voice(msg)))


def media_kind(msg):
    """"фото", "файл" for a file whose text can be read locally, else None."""
    if msg.photo:
        return "фото"
    if is_file(msg) and Path(msg.file.name or "").suffix.lower()[1:] in READABLE:
        return "файл"
    return None


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
        # "]" and ": " in a sender-chosen name would end the tag or fake a description.
        name = (msg.file.name or "").replace("]", ")").replace(": ", " - ")
        tags.append(f"[файл {', '.join(filter(None, [name, size_str(msg.file.size)]))}]")
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


async def read_media(client, msg, path):
    """(state, lines) for a photo or a readable file; downloaded to memory only.
    A file's text goes to text/<id>.txt, not to the archive: the agent reads the archive on every task."""
    if msg.document and msg.file.size > MAX_FILE:
        return ", слишком большой", []
    data = await client.download_media(msg, file=bytes)
    try:
        if msg.photo:
            lines = await asyncio.to_thread(recognize, data)
            return (", с текстом", lines) if lines else (", без текста", [])
        lines, info = await asyncio.to_thread(extract_text, msg.file.name, data)
    except Unreadable:  # broken file; retrying won't help
        return ", не читается", []
    if not lines:
        return f"{info}, без текста", []
    (path / "text").mkdir(exist_ok=True)
    (path / "text" / f"{msg.id}.txt").write_text("\n".join(lines) + "\n")
    return f"{info}, с текстом", []


async def process_media(client, entity, kind, ids=None, root=ARCHIVE):
    start = time.monotonic()
    tool, what = ("ocr_photos", "фото") if kind == "фото" else ("read_files", "файлов")
    path = chat_dir(root, get_peer_id(entity), get_display_name(entity))
    pending = pending_ids(path, kind)
    # Already processed ones are skipped: processing again would duplicate lines and drop a description.
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
                    update_block(path, msg_id, lambda b: set_media(b, ", недоступно"))
                    continue
                if media_kind(msg) != kind:
                    continue
                try:
                    state, lines = await read_media(client, msg, path)
                except FloodWaitError:
                    raise
                except Exception:  # left unprocessed, retried on the next call
                    failed += 1
                    continue
                update_block(path, msg_id, lambda b: set_media(b, state, lines))
                done += 1
    except TimeoutError:
        status = f"не всё, вызови {tool} ещё раз"
    except FloodWaitError as e:
        status = f"Telegram просит подождать {e.seconds} с, потом вызови {tool} ещё раз"
    left = len(pending_ids(path, kind))
    return f"{status}: обработано {done}, ошибок {failed}, осталось необработанных {what} {left}. Папка: {path}"


async def ocr_photos(client, entity, ids=None, root=ARCHIVE):
    return await process_media(client, entity, "фото", ids, root)


async def read_files(client, entity, ids=None, root=ARCHIVE):
    return await process_media(client, entity, "файл", ids, root)


async def download_large(client, msg, folder: Path) -> str:
    """Downloads a file of any size to disk over several calls: each one resumes the .part file."""
    start = time.monotonic()
    dest = folder / f"{msg.id}{msg.file.ext or ''}"
    if dest.exists():
        return f"#{msg.id}: {dest}"
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    with open(part, "ab") as f:
        async for chunk in client.iter_download(msg.media, offset=done):
            f.write(chunk)
            done += len(chunk)
            if done < msg.file.size and time.monotonic() - start > BUDGET_S:
                return f"#{msg.id}: скачано {size_str(done)} из {size_str(msg.file.size)}, вызови fetch_large_file ещё раз"
    part.rename(dest)
    return f"#{msg.id}: {dest}"


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
    new = old = voiced = unvoiced = 0
    auto = {"фото": 0, "файл": 0}
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
                    kind = media_kind(msg)
                    if kind and auto[kind] < MAX_AUTO_MEDIA and time.monotonic() - start < BUDGET_S:
                        try:
                            line = set_media(line, *await read_media(client, msg, path))
                            auto[kind] += 1
                        except FloodWaitError:
                            raise
                        except Exception:  # left unprocessed for ocr_photos / read_files
                            pass
                    buf.setdefault(f"{msg.date.astimezone():%Y-%m}", []).append(line)
                    new += 1
                    if new % FLUSH_EVERY == 0:
                        flush(append)
                    check_budget()
            finally:
                flush(append)
        if target is not None:
            # Old history is not transcribed, OCR'd or read: that is done lazily, when a task needs it.
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
    ocr = f", фото распознано {auto['фото']}" if auto["фото"] else ""
    ocr += f", файлов прочитано {auto['файл']}" if auto["файл"] else ""
    older = f", старых {old}" if old else ""
    return f"{status}: новых сообщений {new}{older}{voice}{ocr}. Папка: {path}"
