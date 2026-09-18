"""Pusat kendali AI Office: menjalankan office.py dari dashboard atau Telegram.

Dipakai dashboard.py. Dua aturan yang menjaga supaya tidak kacau:

1. Satu pipeline saja. office.py menulis `.pipeline.lock` berisi PID-nya dan
   menghapusnya saat selesai; di sini kuncinya dibaca, bukan ditebak.
2. Telegram hanya didengarkan saat TIDAK ada pipeline berjalan. Antrean
   getUpdates milik bot cuma satu dan offset-nya bersama - kalau dua proses
   ikut membaca, jawaban gate Bos bisa tertelan di sini dan hilang.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
WS = ROOT / "workspace"
LOCK = ROOT / ".pipeline.lock"
STAGES = ["ba", "brd", "fsd", "sa", "design", "dev", "qa"]
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Status pipeline
# ---------------------------------------------------------------------------
def running() -> dict | None:
    """Info pipeline yang sedang jalan, atau None. Kunci basi tetap dilaporkan
    apa adanya - lebih baik Bos melihat dan menghapusnya sendiri daripada
    kita diam-diam menimpanya lalu dua pipeline jalan bersamaan."""
    if not LOCK.exists():
        return None
    try:
        return json.loads(LOCK.read_text(encoding="utf-8"))
    except Exception:
        return {"pid": None, "project": "?", "action": "?", "started": 0, "rusak": True}


def clear_lock():
    LOCK.unlink(missing_ok=True)


def stop() -> tuple[bool, str]:
    info = running()
    if not info:
        return False, "Tidak ada pipeline yang berjalan."
    pid = info.get("pid")
    if not pid:
        clear_lock()
        return True, "Kunci rusak, sudah dihapus."
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.kill(pid, 15)
    except Exception as e:
        return False, f"Gagal menghentikan PID {pid}: {e}"
    clear_lock()
    return True, f"Pipeline (PID {pid}) dihentikan."


# ---------------------------------------------------------------------------
# Menjalankan office.py
# ---------------------------------------------------------------------------
def start(project: str, action: str, text: str = "") -> tuple[bool, str]:
    """action: bug | change | resume | lanjut. text: isi laporan / nama tahap."""
    if not SAFE_NAME.match(project or ""):
        return False, "Nama proyek hanya boleh huruf, angka, - dan _."
    if running():
        return False, "Masih ada pipeline berjalan. Hentikan dulu atau tunggu selesai."

    if action == "baru":
        # Proyek baru: namanya ditentukan Bos, bukan dipotong dari ide, jadi tidak
        # ada risiko tabrakan 40 karakter. Menolak nama yang sudah dipakai.
        if (WS / project / "docs" / "STATE.txt").exists():
            return False, f"Proyek '{project}' sudah ada. Pakai nama lain."
        if not text.strip():
            return False, "Ide aplikasinya belum diisi."
        _spawn([sys.executable, "-u", "office.py", text.strip(), "--project", project], project)
        return True, f"Proyek '{project}' dimulai."

    if not (WS / project).exists():
        return False, f"Proyek '{project}' tidak ada di workspace/."

    cmd = [sys.executable, "-u", "office.py", "--project", project]
    if action == "bug":
        if not text.strip():
            return False, "Laporan bug tidak boleh kosong."
        cmd += ["--bug", text.strip()]
    elif action == "change":
        if not text.strip():
            return False, "Permintaan perubahan tidak boleh kosong."
        cmd += ["--change", text.strip()]
    elif action == "resume":
        if text not in STAGES:
            return False, f"Tahap harus salah satu dari: {', '.join(STAGES)}."
        cmd += ["--resume", text]
    elif action != "lanjut":
        return False, f"Aksi '{action}' tidak dikenal."

    _spawn(cmd, project)
    return True, f"Dijalankan: {' '.join(cmd[2:])}"


def _spawn(cmd: list, project: str):
    """Jalankan office.py terlepas dari dashboard, keluaran ke docs/pipeline.log."""
    log = WS / project / "docs" / "pipeline.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    f = log.open("a", encoding="utf-8")
    f.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} :: {' '.join(cmd[2:])} =====\n")
    f.flush()
    # CREATE_NEW_PROCESS_GROUP / start_new_session supaya pipeline tidak ikut mati
    # kalau dashboard-nya ditutup.
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
          else {"start_new_session": True})
    subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, **kw)


# ---------------------------------------------------------------------------
# Bahan referensi dari pemilik produk
# ---------------------------------------------------------------------------
REF_EXT = {".md", ".txt", ".json", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}
REF_MAKS = 10 * 1024 * 1024


def buat_proyek(project: str, ide: str) -> tuple[bool, str]:
    """Siapkan folder proyek TANPA menjalankan pipeline, supaya bahan acuan bisa
    diunggah dulu. Pipeline dimulai belakangan lewat aksi 'lanjut', yang membaca
    ide dari docs/IDEA.md dan mulai dari tahap BA karena STATE.txt belum ada."""
    if not SAFE_NAME.match(project or ""):
        return False, "Nama proyek hanya boleh huruf, angka, - dan _."
    if not (ide or "").strip():
        return False, "Ide aplikasinya belum diisi."
    docs = WS / project / "docs"
    if (docs / "STATE.txt").exists():
        return False, f"Proyek '{project}' sudah pernah dijalankan. Pakai nama lain."
    (docs / "referensi").mkdir(parents=True, exist_ok=True)
    (docs / "IDEA.md").write_text(ide.strip(), encoding="utf-8")
    return True, f"Proyek '{project}' siap. Unggah bahan acuan, lalu mulai pipeline."


def dir_referensi(project: str) -> Path:
    return WS / project / "docs" / "referensi"


def simpan_referensi(project: str, nama: str, data: bytes) -> tuple[bool, str]:
    """Simpan satu berkas acuan. Tahap pipeline tetap berjalan seperti biasa;
    berkas ini dibaca peran-peran sebagai acuan, bukan pengganti tahap."""
    if not SAFE_NAME.match(project or ""):
        return False, "Nama proyek tidak valid."
    # Path(...).name membuang direktori, jadi "../../.env" tidak bisa keluar folder.
    nama = Path(nama or "").name
    if not nama or nama.startswith("."):
        return False, "Nama berkas tidak valid."
    ext = Path(nama).suffix.lower()
    if ext not in REF_EXT:
        return False, f"Jenis berkas '{ext or 'tanpa ekstensi'}' tidak didukung. Boleh: {', '.join(sorted(REF_EXT))}"
    if len(data) > REF_MAKS:
        return False, f"Berkas terlalu besar ({len(data) // 1024} KB, batas {REF_MAKS // 1024 // 1024} MB)."
    if not data:
        return False, "Berkas kosong."
    d = dir_referensi(project)
    d.mkdir(parents=True, exist_ok=True)
    (d / nama).write_bytes(data)
    return True, f"'{nama}' tersimpan sebagai acuan ({len(data) // 1024 or 1} KB)."


def daftar_referensi(project: str) -> list:
    d = dir_referensi(project)
    if not SAFE_NAME.match(project or "") or not d.exists():
        return []
    return sorted(
        ({"nama": f.name, "byte": f.stat().st_size} for f in d.iterdir() if f.is_file()),
        key=lambda x: x["nama"])


def hapus_referensi(project: str, nama: str) -> tuple[bool, str]:
    nama = Path(nama or "").name
    f = dir_referensi(project) / nama
    if not SAFE_NAME.match(project or "") or not nama or not f.is_file():
        return False, "Berkas tidak ditemukan."
    f.unlink()
    return True, f"'{nama}' dihapus."


# ---------------------------------------------------------------------------
# Perintah lewat Telegram - HANYA saat tidak ada pipeline berjalan
# ---------------------------------------------------------------------------
_offset = 0

HELP = ("Perintah AI Office (hanya saat tidak ada pipeline berjalan):\n"
        "/status - keadaan sekarang\n"
        "/bug <teks> - laporkan bug, langsung diperbaiki lalu diuji ulang\n"
        "/ubah <teks> - permintaan fitur/perubahan (dokumen ikut diperbarui)\n"
        "/lanjut - lanjutkan dari tahap terakhir\n"
        "/resume <tahap> - mulai dari tahap tertentu (ba|brd|fsd|sa|dev|qa)\n"
        "/stop - hentikan pipeline yang berjalan\n"
        "/proyek <nama> - pilih proyek yang dikendalikan\n"
        "/baru <nama> <ide aplikasi> - mulai proyek baru\n\n"
        "Saat pipeline berjalan, chat ini dipakai gate: balas y / q / teks feedback.")


def _tg(method: str, **params):
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if not tok:
        return None
    try:
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/{method}",
                                    data, timeout=20) as r:
            return json.load(r)
    except Exception:
        return None


def say(text: str):
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if chat:
        _tg("sendMessage", chat_id=chat, text=text[:3900])


def default_project() -> str | None:
    ds = sorted(WS.glob("*/docs"), key=lambda d: d.stat().st_mtime, reverse=True)
    return ds[0].parent.name if ds else None


def handle(text: str, project: str | None) -> tuple[str, str | None]:
    """Jalankan satu perintah. Mengembalikan (balasan, proyek terpilih)."""
    text = text.strip()
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower().lstrip("/"), arg.strip()

    if cmd in ("start", "help", "bantuan"):
        return HELP, project
    if cmd == "proyek":
        if not SAFE_NAME.match(arg or "") or not (WS / arg).exists():
            ada = ", ".join(d.parent.name for d in sorted(WS.glob("*/docs"))) or "(kosong)"
            return f"Proyek tidak ditemukan. Yang ada: {ada}", project
        return f"Proyek aktif sekarang: {arg}", arg

    project = project or default_project()
    if not project:
        return "Belum ada proyek di workspace/.", None

    if cmd == "status":
        r = running()
        sp = WS / project / "docs" / "status.json"
        st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
        v = st.get("verdict") or {}
        return (f"Proyek: {project}\n"
                f"Tahap: {st.get('stage', '?')}\n"
                f"Pipeline: {'berjalan (PID ' + str(r.get('pid')) + ')' if r else 'tidak berjalan'}\n"
                f"Verdict terakhir: QA={v.get('qa', '-')}, Pentest={v.get('pentest', '-')}\n"
                f"Biaya: ${(st.get('cost') or {}).get('total', 0):.2f}"), project
    if cmd == "stop":
        return stop()[1], project
    if cmd in ("baru", "proyekbaru"):
        nama, _, ide = arg.partition(" ")
        ok, msg = start(nama, "baru", ide)
        return msg, (nama if ok else project)
    if cmd in ("bug", "ubah", "change", "lanjut", "resume"):
        aksi = {"bug": "bug", "ubah": "change", "change": "change",
                "lanjut": "lanjut", "resume": "resume"}[cmd]
        ok, msg = start(project, aksi, arg)
        return msg, project
    return f"Perintah '{cmd}' tidak dikenal.\n\n{HELP}", project


def _loop():
    global _offset
    project = None
    while True:
        try:
            # Pipeline berjalan = chat milik gate. Jangan sentuh antreannya.
            if running():
                time.sleep(3)
                continue
            res = _tg("getUpdates", timeout=0, offset=_offset)
            chat = os.getenv("TELEGRAM_CHAT_ID")
            for u in (res or {}).get("result", []):
                _offset = u["update_id"] + 1
                m = u.get("message") or {}
                t = (m.get("text") or "").strip()
                if not t or str(m.get("chat", {}).get("id")) != str(chat):
                    continue
                if not t.startswith("/"):
                    continue          # teks biasa bukan perintah: abaikan
                reply, project = handle(t, project)
                say(reply)
        except Exception:
            pass
        time.sleep(3)


def start_telegram_listener():
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        return False
    threading.Thread(target=_loop, daemon=True).start()
    return True
