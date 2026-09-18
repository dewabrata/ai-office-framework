"""Monitoring: tulis event ke docs/events.jsonl + snapshot docs/status.json,
kirim notifikasi Telegram, dan terima jawaban gate dari Telegram atau terminal.
Tidak ada dependency di luar stdlib.
"""
import asyncio
import json
import os
import queue
import threading
import time
import uuid
import urllib.parse
import urllib.request
from pathlib import Path

_docs: Path | None = None
_status: dict = {}
_tg_offset = 0
# Biaya kumulatif terakhir per tahap. ResultMessage terbit sekali per subagent
# dan tiap kali membawa total tahap, bukan tambahan - jadi yang boleh
# ditambahkan hanya selisihnya.
_run_cost: dict = {}


# ---------------------------------------------------------------------------
# Init / event log
# ---------------------------------------------------------------------------
def baca_json(path: Path) -> dict:
    """Baca JSON sebagai UTF-8; berkas lama yang tertulis cp1252 tetap terbaca."""
    if not path.exists():
        return {}
    data = path.read_bytes()
    for enc in ("utf-8", "cp1252"):
        try:
            return json.loads(data.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return {}


def init(docs: Path, project: str):
    global _docs, _status
    _docs = docs
    sp = docs / "status.json"
    _status = baca_json(sp)
    _status.update(project=project, updated=time.time())
    _status.setdefault("cost", {})
    _status.setdefault("quota", {})
    _save()


def _save():
    if _docs:
        _status["updated"] = time.time()
        # Wajib UTF-8: bawaan Windows cp1252 tidak punya karakter seperti panah,
        # dan teks gate/KESENJANGAN sering memuatnya - tanpa ini pipeline crash.
        (_docs / "status.json").write_text(json.dumps(_status, ensure_ascii=False, indent=1),
                                           encoding="utf-8")


def emit(kind: str, **data):
    """Catat satu event. kind: stage_start|stage_end|agent|subagent|tool|gate|gate_answer|
    quota|cost|verdict|error|info"""
    if kind == "stage_start":
        _run_cost.pop(data.get("label"), None)
    if not _docs:
        return
    ev = {"t": time.time(), "kind": kind, **data}
    with (_docs / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def set_status(**kw):
    _status.update(kw)
    _save()


def add_cost(label: str, usd: float):
    """Tambahkan hanya selisih dari laporan biaya sebelumnya pada tahap yang sama.

    Tanpa ini tahap ber-subagent terhitung berlipat: tahap LEAD dengan 9 subagent
    mencatat 9 x biaya penuh.
    """
    c = _status["cost"]
    delta = usd - _run_cost.get(label, 0.0)
    _run_cost[label] = usd
    if delta <= 0:
        return
    c[label] = round(c.get(label, 0) + delta, 4)
    c["total"] = round(sum(v for k, v in c.items() if k != "total"), 4)
    _save()


def set_quota(**kw):
    _status["quota"].update(kw)
    _save()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def _tg_cfg():
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    return (tok, chat) if tok and chat else (None, None)


def tg_send(text: str):
    tok, chat = _tg_cfg()
    if not tok:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=10)
    except Exception as e:  # notifikasi tidak boleh menjatuhkan pipeline
        print(f"  (telegram gagal: {e})")


def tg_doc(path, caption: str = ""):
    """Kirim satu berkas ke Telegram sebagai dokumen.

    Berkas .md dikirim dengan nama .txt supaya Telegram menampilkan isinya
    langsung di aplikasi; kalau tetap .md, Telegram memaksa unduh dan minta
    aplikasi lain. Sama seperti tg_send, kegagalan tidak menjatuhkan pipeline.
    """
    tok, chat = _tg_cfg()
    path = Path(path)
    if not tok or not path.exists():
        return
    size = path.stat().st_size
    if size == 0:
        return
    if size > 45 * 1024 * 1024:          # batas Telegram 50 MB
        tg_send(f"{path.name} terlalu besar ({size // 1024} KB). Buka lewat dashboard.")
        return

    name = path.stem + ".txt" if path.suffix == ".md" else path.name
    b = uuid.uuid4().hex
    crlf = chr(13) + chr(10)

    def field(n, v):
        return (f"--{b}{crlf}Content-Disposition: form-data; "
                f'name="{n}"{crlf}{crlf}{v}{crlf}').encode()

    body = field("chat_id", chat)
    if caption:
        body += field("caption", caption[:1000])
    body += (f"--{b}{crlf}Content-Disposition: form-data; "
             f'name="document"; filename="{name}"{crlf}'
             f"Content-Type: text/plain; charset=utf-8{crlf}{crlf}").encode()
    body += path.read_bytes() + f"{crlf}--{b}--{crlf}".encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendDocument", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        print(f"  (telegram gagal kirim {name}: {e})")


def _tg_poll() -> str | None:
    """Ambil satu pesan teks baru dari chat yang dikonfigurasi. None kalau tidak ada."""
    global _tg_offset
    tok, chat = _tg_cfg()
    if not tok:
        return None
    try:
        url = f"https://api.telegram.org/bot{tok}/getUpdates?timeout=0&offset={_tg_offset}"
        with urllib.request.urlopen(url, timeout=10) as r:
            for u in json.load(r).get("result", []):
                _tg_offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) == str(chat) and m.get("text"):
                    return m["text"].strip()
    except Exception:
        pass
    return None


def tg_drain():
    """Buang pesan lama supaya jawaban gate tidak diambil dari pesan sebelum pipeline jalan."""
    while _tg_poll() is not None:
        pass


# ---------------------------------------------------------------------------
# Gate: tunggu jawaban dari terminal ATAU Telegram
# ---------------------------------------------------------------------------
_stdin_q: queue.Queue = queue.Queue()
_stdin_started = False


def _stdin_reader():
    while True:
        try:
            _stdin_q.put(input())
        except EOFError:
            return


async def ask(question: str, label: str, files: list | None = None) -> str:
    global _stdin_started
    print("\n" + "=" * 70 + f"\n{question}\n"
          "Ketik 'y' untuk setuju, 'q' untuk berhenti, atau tulis feedback revisi.\n"
          + "=" * 70)
    emit("gate", label=label, question=question)
    set_status(gate={"label": label, "question": question, "since": time.time()})
    tg_drain()
    # Lampirkan dokumennya dulu, pertanyaan belakangan, supaya pertanyaan
    # jadi pesan terakhir dan paling terlihat di chat.
    for f in files or []:
        tg_doc(f, f"{label}: {Path(f).name} - baca dulu, lalu jawab pertanyaan di bawah.")
    tg_send(f"⏸ {label}\n{question}\n\nBalas: y (setuju) / q (berhenti) / teks feedback")

    if not _stdin_started:
        threading.Thread(target=_stdin_reader, daemon=True).start()
        _stdin_started = True

    while True:
        try:
            ans = _stdin_q.get_nowait().strip()
            src = "terminal"
        except queue.Empty:
            ans, src = _tg_poll(), "telegram"
        if ans:
            print(f"> [{src}] {ans}")
            emit("gate_answer", label=label, answer=ans, source=src)
            set_status(gate=None)
            if src == "telegram":
                tg_send(f"✅ diterima: {ans[:200]}")
            return ans
        await asyncio.sleep(2)
