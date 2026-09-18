"""Dashboard web lokal untuk memantau AI Office secara realtime.
Hanya stdlib. Jalankan:  python dashboard.py   ->  http://localhost:8765
Membaca workspace/<project>/docs/{status.json, events.jsonl, TASKS.md, *_REPORT.md, FIXES.md}.
"""
import base64
import hmac
import http.cookies
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

import control
from monitor import baca_json

load_dotenv()

ROOT = Path(__file__).parent
WS = ROOT / "workspace"
PORT = int(os.getenv("DASHBOARD_PORT", 8765))
DOCS_ALLOWED = {"PRD", "BRD", "FSD", "SPEC", "TASKS", "QA_REPORT", "PENTEST_REPORT", "FIXES", "IDEA"}
STAGES = ["ba", "brd", "fsd", "sa", "design", "dev", "qa", "done"]

# --- Akses ---------------------------------------------------------------
# Dashboard ini bukan pemantau pasif: POST /api/run menyuntikkan teks bebas ke
# dokumen yang dibaca agent bertool Bash. Tanpa penjagaan, siapa pun yang bisa
# menjangkau port ini bisa menjalankan kode di mesin ini. Karena itu bawaannya
# hanya loopback, dan membuka ke jaringan WAJIB pakai kata sandi.
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
USER = os.getenv("DASHBOARD_USER", "admin").strip() or "admin"
PASS = os.getenv("DASHBOARD_PASS", "")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def butuh_sandi() -> bool:
    return HOST not in LOOPBACK


def periksa_akses(header: str) -> bool:
    """True kalau permintaan boleh dilayani."""
    if not PASS:
        return not butuh_sandi()      # tanpa sandi: hanya sah di loopback
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    # compare_digest supaya lama pembandingan tidak membocorkan isi sandi
    return hmac.compare_digest(user, USER) and hmac.compare_digest(pw, PASS)


# --- Sesi login ----------------------------------------------------------
# Basic auth tetap diterima untuk curl/skrip, tapi orang dapat halaman login
# sungguhan dengan cookie sesi: ada tombol keluar, dan sandinya tidak dikirim
# ulang di setiap permintaan.
SESI: dict = {}                       # token -> waktu kedaluwarsa
SESI_TTL = 12 * 3600
GAGAL: dict = {}                      # ip -> daftar waktu percobaan gagal
MAKS_GAGAL, JENDELA_GAGAL = 5, 300


def sesi_baru() -> str:
    sekarang = time.time()
    for t, exp in list(SESI.items()):  # buang yang mati supaya tidak menumpuk
        if exp < sekarang:
            SESI.pop(t, None)
    token = secrets.token_urlsafe(32)
    SESI[token] = sekarang + SESI_TTL
    return token


def sesi_sah(cookie_header: str) -> bool:
    c = http.cookies.SimpleCookie()
    try:
        c.load(cookie_header or "")
    except Exception:
        return False
    m = c.get("sesi")
    if not m:
        return False
    exp = SESI.get(m.value)
    return bool(exp and exp > time.time())


def sesi_hapus(cookie_header: str):
    c = http.cookies.SimpleCookie()
    try:
        c.load(cookie_header or "")
    except Exception:
        return
    m = c.get("sesi")
    if m:
        SESI.pop(m.value, None)


def terkunci(ip: str) -> int:
    """Sisa detik penguncian setelah terlalu banyak percobaan gagal, 0 kalau bebas."""
    batas = time.time() - JENDELA_GAGAL
    riwayat = [t for t in GAGAL.get(ip, []) if t > batas]
    GAGAL[ip] = riwayat
    if len(riwayat) < MAKS_GAGAL:
        return 0
    return int(riwayat[0] + JENDELA_GAGAL - time.time()) + 1


def catat_gagal(ip: str):
    GAGAL.setdefault(ip, []).append(time.time())


LOGIN_HTML = r"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Masuk — AI Office</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;
 color:#e6edf3;font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif}
.kartu{width:min(92vw,380px);background:#11161d;border:1px solid #232c38;border-radius:12px;
 padding:28px 26px 24px}
h1{margin:0 0 4px;font-size:19px;font-weight:600}
.sub{margin:0 0 22px;color:#7d8894;font-size:13px}
label{display:block;margin:14px 0 6px;font-size:13px;color:#b6c2cf}
input{width:100%;background:#0d1117;color:#e6edf3;border:1px solid #232c38;border-radius:8px;
 padding:10px 12px;font:inherit}
input:focus{outline:none;border-color:#1f6feb}
button{width:100%;margin-top:20px;background:#1f6feb;color:#fff;border:0;border-radius:8px;
 padding:11px;font:inherit;font-weight:500;cursor:pointer}
button:hover{filter:brightness(1.12)}
.galat{margin-top:16px;padding:10px 12px;border-radius:8px;background:#2d1618;
 border:1px solid #5c2b2b;color:#ff9b93;font-size:13px}
.kaki{margin-top:18px;color:#6b7681;font-size:12px;line-height:1.6}
</style></head><body>
<form class="kartu" method="POST" action="/login">
  <h1>AI Office</h1>
  <p class="sub">Masuk untuk memantau dan menjalankan pipeline.</p>
  <label for="u">Pengguna</label>
  <input id="u" name="user" autocomplete="username" autofocus>
  <label for="p">Sandi</label>
  <input id="p" name="pass" type="password" autocomplete="current-password">
  <button type="submit">Masuk</button>
  __GALAT__
  <p class="kaki">Kredensial diatur lewat DASHBOARD_USER dan DASHBOARD_PASS di .env.</p>
</form></body></html>"""


def halaman_login(galat: str = "") -> bytes:
    blok = f'<div class="galat">{galat}</div>' if galat else ""
    return LOGIN_HTML.replace("__GALAT__", blok).encode("utf-8")


def projects():
    """Daftar proyek beserta KONDISI-nya, supaya tampilan bisa menawarkan aksi
    yang tepat: baru -> mulai, berhenti -> lanjutkan, berjalan -> hentikan,
    selesai -> lapor bug / minta fitur."""
    jalan = control.running() or {}
    out = []
    for d in WS.glob("*/docs"):
        nama = d.parent.name
        st = baca_json(d / "status.json")
        f_state = d / "STATE.txt"
        state = f_state.read_text(encoding="utf-8", errors="replace").strip() if f_state.exists() else ""
        f_ide = d / "IDEA.md"
        ide = f_ide.read_text(encoding="utf-8", errors="replace").strip() if f_ide.exists() else ""
        ref = d / "referensi"
        if jalan.get("project") == nama:
            kondisi = "gate" if st.get("gate") else "berjalan"
        elif state == "done":
            kondisi = "selesai"
        elif state:
            kondisi = "berhenti"
        else:
            kondisi = "baru"
        waktu = st.get("updated") or max((f.stat().st_mtime for f in d.iterdir() if f.is_file()), default=0)
        out.append({
            "name": nama, "kondisi": kondisi, "stage": state or None,
            "idea": ide[:220], "refs": sum(1 for f in ref.iterdir() if f.is_file()) if ref.exists() else 0,
            "updated": waktu, "cost": (st.get("cost") or {}).get("total", 0),
            "verdict": st.get("verdict"), "gate": (st.get("gate") or {}).get("label"),
        })
    out.sort(key=lambda x: x["updated"] or 0, reverse=True)
    return out


def tasks(docs: Path):
    f = docs / "TASKS.md"
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.match(r"^[A-Z]+-?\d+", cells[0]):
            rows.append({"id": cells[0], "title": cells[1], "file": cells[2], "status": cells[3].upper(),
                         "note": cells[4] if len(cells) > 4 else ""})
    return rows


def events(docs: Path, offset: int):
    f = docs / "events.jsonl"
    if not f.exists():
        return [], 0
    data = f.read_bytes()
    chunk = data[offset:]
    lines = [json.loads(l) for l in chunk.decode("utf-8", "ignore").splitlines() if l.strip()]
    return lines, len(data)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # diam
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _redirect(self, ke: str, cookie: str = ""):
        self.send_response(302)
        self.send_header("Location", ke)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _kirim_login(self, galat: str = "", kode: int = 200):
        body = halaman_login(galat)
        self.send_response(kode)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _boleh(self) -> bool:
        """True kalau permintaan boleh dilayani; kalau tidak, balasannya sudah dikirim."""
        if not PASS and not butuh_sandi():
            return True                      # lokal tanpa sandi: sesuai setelan
        if sesi_sah(self.headers.get("Cookie", "")):
            return True
        if periksa_akses(self.headers.get("Authorization", "")):
            return True                      # jalur curl/skrip
        if self.path.startswith("/api/"):
            self._json({"error": "belum login"}, 401)
        else:
            self._redirect("/login")
        return False

    def _proses_login(self):
        sisa = terkunci(self._ip())
        if sisa:
            return self._kirim_login(
                f"Terlalu banyak percobaan gagal. Coba lagi dalam {sisa} detik.", 429)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._kirim_login("Data tidak terbaca.", 400)
        user = (form.get("user") or [""])[0]
        sandi = (form.get("pass") or [""])[0]
        if hmac.compare_digest(user, USER) and PASS and hmac.compare_digest(sandi, PASS):
            token = sesi_baru()
            self._redirect("/", f"sesi={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESI_TTL}")
        else:
            catat_gagal(self._ip())
            self._kirim_login("Pengguna atau sandi salah.", 401)

    def do_GET(self):
        if urlparse(self.path).path == "/login":
            if sesi_sah(self.headers.get("Cookie", "")):
                return self._redirect("/")
            return self._kirim_login()
        if urlparse(self.path).path == "/logout":
            sesi_hapus(self.headers.get("Cookie", ""))
            return self._redirect("/login", "sesi=; Path=/; Max-Age=0")
        if not self._boleh():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())
            return
        if u.path == "/api/projects":
            return self._json(projects())
        if u.path == "/api/refs":
            proj = re.sub(r"[^a-zA-Z0-9_-]", "", q.get("project", ""))
            return self._json({"refs": control.daftar_referensi(proj)})
        if u.path == "/api/running":
            return self._json({"running": control.running()})
        proj = re.sub(r"[^a-zA-Z0-9_-]", "", q.get("project", ""))
        docs = WS / proj / "docs"
        if not proj or not docs.exists():
            return self._json({"error": "project tidak ditemukan"}, 404)
        if u.path == "/api/status":
            st = baca_json(docs / "status.json")
            st["tasks"] = tasks(docs)
            st["docs"] = sorted(p.stem for p in docs.glob("*.md") if p.stem in DOCS_ALLOWED)
            return self._json(st)
        if u.path == "/api/events":
            ev, off = events(docs, int(q.get("offset", 0)))
            return self._json({"events": ev[-400:], "offset": off})
        if u.path == "/api/doc":
            name = q.get("name", "")
            if name not in DOCS_ALLOWED:
                return self._json({"error": "nama dokumen tidak valid"}, 400)
            f = docs / f"{name}.md"
            return self._json({"name": name, "text": f.read_text(encoding="utf-8") if f.exists() else ""})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path == "/login":
            return self._proses_login()
        if not self._boleh():
            return
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"ok": False, "msg": "body bukan JSON"}, 400)

        if u.path == "/api/run":
            ok, msg = control.start(body.get("project", ""), body.get("action", ""),
                                    body.get("text", ""))
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        if u.path == "/api/create-project":
            ok, msg = control.buat_proyek(body.get("project", ""), body.get("text", ""))
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        if u.path == "/api/upload":
            try:
                data = base64.b64decode(body.get("b64", ""), validate=True)
            except Exception:
                return self._json({"ok": False, "msg": "Isi berkas tidak terbaca."}, 400)
            ok, msg = control.simpan_referensi(body.get("project", ""),
                                               body.get("nama", ""), data)
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        if u.path == "/api/ref-delete":
            ok, msg = control.hapus_referensi(body.get("project", ""), body.get("nama", ""))
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        if u.path == "/api/stop":
            ok, msg = control.stop()
            return self._json({"ok": ok, "msg": msg})
        if u.path == "/api/clear-lock":
            control.clear_lock()
            return self._json({"ok": True, "msg": "Kunci dihapus."})
        self._json({"error": "not found"}, 404)


HTML = r"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Office</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#0b0f14; --panel:#11161d; --panel2:#161d27; --line:#222b36;
 --text:#e6edf3; --dim:#8b97a5; --faint:#5f6b7a;
 --accent:#2f81f7; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
 --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px;
 --r:10px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font:14px/1.55 "IBM Plex Sans",system-ui,sans-serif}
a{color:inherit}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace}

/* ---- kerangka ---- */
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh}
aside{background:var(--panel);border-right:1px solid var(--line);
 display:flex;flex-direction:column;padding:var(--s4) var(--s3)}
.brand{display:flex;align-items:center;gap:var(--s2);padding:0 var(--s2) var(--s4);
 font-weight:600;font-size:15px;letter-spacing:.2px}
.brand i{width:10px;height:10px;border-radius:3px;background:var(--accent);display:inline-block}
.pick{margin-bottom:var(--s5)}
.pick label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.6px;
 color:var(--faint);margin:0 var(--s2) var(--s1)}
select,input,textarea{background:var(--bg);color:var(--text);border:1px solid var(--line);
 border-radius:8px;padding:8px 10px;font:inherit;width:100%}
select:focus,input:focus,textarea:focus{outline:none;border-color:var(--accent)}
nav{display:flex;flex-direction:column;gap:2px}
nav a{display:flex;align-items:center;justify-content:space-between;gap:var(--s2);
 padding:9px var(--s3);border-radius:8px;text-decoration:none;color:var(--dim);font-size:13.5px}
nav a:hover{background:var(--panel2);color:var(--text)}
nav a.on{background:var(--accent);color:#fff;font-weight:500}
nav a .tag{font-size:11px;padding:1px 7px;border-radius:20px;background:#0006}
nav a.on .tag{background:#fff3;color:#fff}
.sidefoot{margin-top:auto;padding-top:var(--s4);border-top:1px solid var(--line);
 display:flex;align-items:center;justify-content:space-between;font-size:12px;color:var(--faint)}
.sidefoot a{text-decoration:none;color:var(--dim)}
.sidefoot a:hover{color:var(--text)}

main{padding:var(--s5) var(--s6);max-width:1180px}
.top{display:flex;align-items:baseline;gap:var(--s3);margin-bottom:var(--s5);flex-wrap:wrap}
.top h1{margin:0;font-size:20px;font-weight:600}
.top .sub{color:var(--faint);font-size:13px}
.chips{display:flex;gap:var(--s2);margin-left:auto;flex-wrap:wrap}
.chip{font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid var(--line);
 background:var(--panel);color:var(--dim)}
.chip.live{border-color:#1d4a86;background:#0d2340;color:#79b8ff}
.chip.bad{border-color:#5c2b2b;background:#2d1618;color:#ff9b93}

.page{display:none}
.page.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
 padding:var(--s4) var(--s5);margin-bottom:var(--s4)}
.card h2{margin:0 0 var(--s1);font-size:14px;font-weight:600}
.card p.hint{margin:0 0 var(--s4);color:var(--faint);font-size:12.5px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:var(--s4)}
.row{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}
.row+.row{margin-top:var(--s3)}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;
 font:inherit;font-weight:500;cursor:pointer;white-space:nowrap}
button:hover{filter:brightness(1.12)}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--dim)}
button.ghost:hover{color:var(--text);border-color:#37414f;filter:none}
button.danger{background:#8b2b2b}
button:disabled{background:#232b36;color:var(--faint);cursor:not-allowed;filter:none}
.msg{margin-top:var(--s3);font-size:13px;min-height:20px}
.msg.ok{color:var(--ok)} .msg.bad{color:var(--bad)} .msg.wait{color:var(--dim)}
</style></head>
<style>
/* ---- tahap ---- */
.steps{display:flex;gap:var(--s1);overflow-x:auto;padding-bottom:var(--s1)}
.step{flex:1;min-width:104px;background:var(--panel2);border:1px solid var(--line);
 border-radius:8px;padding:10px 12px}
.step b{display:block;font-size:12.5px;font-weight:500}
.step small{color:var(--faint);font-size:11px}
.step.done{border-color:#1f4a2c}.step.done b{color:var(--ok)}
.step.live{border-color:var(--accent);background:#0d2340}.step.live b{color:#79b8ff}
.step.skip{border-style:dashed;opacity:.6}.step.skip small::after{content:" · dilewati"}

/* ---- gate ---- */
.gate{display:none;border:1px solid #4a3a10;background:#241d08;border-radius:var(--r);
 padding:var(--s4) var(--s5);margin-bottom:var(--s4)}
.gate.show{display:block}
.gate b{color:#e3b341}
.gate .q{margin-top:var(--s2);white-space:pre-wrap}

/* ---- tabel ---- */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--faint);font-weight:500;font-size:11px;
 text-transform:uppercase;letter-spacing:.5px;padding:0 var(--s3) var(--s2) 0;
 border-bottom:1px solid var(--line)}
td{padding:9px var(--s3) 9px 0;border-bottom:1px solid #1a222c;vertical-align:top}
tr:last-child td{border-bottom:0}
.st{font-size:11px;padding:2px 9px;border-radius:20px;white-space:nowrap}
.st.DONE{background:#132d1c;color:var(--ok)}
.st.IN_PROGRESS{background:#0d2340;color:#79b8ff}
.st.TODO{background:#1b222c;color:var(--dim)}
.st.BLOCKED{background:#2d1618;color:#ff9b93}

/* ---- meter ---- */
.kv{display:grid;grid-template-columns:1fr auto;gap:6px var(--s4);font-size:13px}
.kv span:nth-child(odd){color:var(--dim)}
.bar{height:6px;background:#1b222c;border-radius:20px;overflow:hidden;margin:var(--s3) 0 var(--s2)}
.bar i{display:block;height:100%;background:var(--accent);border-radius:20px}
.bar.warn i{background:var(--warn)} .bar.bad i{background:var(--bad)}

/* ---- log ---- */
.log{background:#080b0f;border:1px solid var(--line);border-radius:var(--r);
 padding:var(--s3);height:62vh;overflow:auto;font-family:"IBM Plex Mono",monospace;font-size:12.5px}
.log div{padding:2px 0;border-bottom:1px solid #11161d;word-break:break-word}
.log .t{color:var(--faint);margin-right:var(--s2)}
.k-stage_start{color:#79b8ff} .k-stage_end{color:var(--ok)} .k-error{color:var(--bad)}
.k-gate{color:#e3b341} .k-gate_answer{color:#e3b341} .k-tool{color:var(--faint)}
.k-subagent{color:#c093f5} .k-verdict{color:#e3b341} .k-model{color:var(--dim)}

/* ---- dokumen ---- */
.docwrap{display:grid;grid-template-columns:180px 1fr;gap:var(--s4)}
.doclist{display:flex;flex-direction:column;gap:2px}
.doclist button{background:transparent;border:0;color:var(--dim);text-align:left;
 padding:7px var(--s3);border-radius:8px;font-weight:400}
.doclist button:hover{background:var(--panel2);color:var(--text);filter:none}
.doclist button.on{background:var(--panel2);color:var(--text);font-weight:500}
.doc{background:#080b0f;border:1px solid var(--line);border-radius:var(--r);padding:var(--s4);
 height:66vh;overflow:auto;white-space:pre-wrap;font-family:"IBM Plex Mono",monospace;font-size:12.5px}

/* ---- unggah ---- */
.drop{border:2px dashed var(--line);border-radius:var(--r);padding:var(--s6);text-align:center;
 color:var(--dim);cursor:pointer;transition:.15s}
.drop:hover,.drop.over{border-color:var(--accent);background:#0d2340;color:var(--text)}
.drop b{display:block;font-size:15px;color:var(--text);margin-bottom:var(--s1)}
.drop small{color:var(--faint)}
.files{margin-top:var(--s4);display:flex;flex-direction:column;gap:var(--s2)}
.file{display:flex;align-items:center;gap:var(--s3);background:var(--panel2);
 border:1px solid var(--line);border-radius:8px;padding:9px var(--s3);font-size:13px}
.file .nm{flex:1;word-break:break-all}
.file .sz{color:var(--faint);font-size:12px}
.file button{background:transparent;color:var(--faint);border:0;padding:2px 6px;font-size:16px;line-height:1}
.file button:hover{color:var(--bad);filter:none}
.kosong{color:var(--faint);font-size:13px;padding:var(--s4) 0}
@media(max-width:820px){
 .app{grid-template-columns:1fr} aside{border-right:0;border-bottom:1px solid var(--line)}
 nav{flex-direction:row;flex-wrap:wrap} main{padding:var(--s4)}
 .docwrap{grid-template-columns:1fr}
}
</style>
<style>
/* ---- beranda proyek ---- */
.proyek-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:var(--s4)}
.pk{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:var(--s4) var(--s5);
 display:flex;flex-direction:column;gap:var(--s2);text-decoration:none;color:inherit;min-height:176px}
.pk:hover{border-color:#37414f;background:var(--panel2)}
.pk .nama{font-size:15px;font-weight:600;word-break:break-word}
.pk .ide{color:var(--dim);font-size:13px;flex:1;display:-webkit-box;-webkit-line-clamp:3;
 -webkit-box-orient:vertical;overflow:hidden}
.pk .baris{display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap;font-size:12px;color:var(--faint)}
.pk .baris .kanan{margin-left:auto}
.pk.tambah{border-style:dashed;align-items:center;justify-content:center;text-align:center}
.pk.tambah .plus{font-size:30px;line-height:1;color:var(--accent)}
.badge{font-size:11.5px;padding:2px 10px;border-radius:20px;white-space:nowrap;border:1px solid transparent}
.badge.baru{background:#1b222c;color:var(--dim)}
.badge.berjalan{background:#0d2340;color:#79b8ff;border-color:#1d4a86}
.badge.gate{background:#241d08;color:#e3b341;border-color:#4a3a10}
.badge.berhenti{background:#2a1f0b;color:#d29922;border-color:#4a3a10}
.badge.selesai{background:#132d1c;color:var(--ok);border-color:#1f4a2c}

/* ---- panel aksi proyek ---- */
.aksi{display:flex;align-items:center;gap:var(--s4);background:var(--panel);border:1px solid var(--line);
 border-radius:var(--r);padding:var(--s4) var(--s5);margin-bottom:var(--s4);flex-wrap:wrap}
.aksi .teks{flex:1;min-width:260px}
.aksi .teks .jd{display:block;font-size:15px;font-weight:600;margin-bottom:4px}
.aksi .teks .ket{color:var(--dim);font-size:13px;white-space:pre-wrap}
.aksi .teks .kecil{display:block;margin-top:6px;color:var(--faint);font-size:12.5px}
.aksi.gate{border-color:#4a3a10;background:#1c1707}
.aksi.berjalan{border-color:#1d4a86;background:#0b1a2e}
.aksi.berhenti{border-color:#4a3a10}
.aksi.selesai{border-color:#1f4a2c}
.aksi .catatan{width:100%;color:var(--faint);font-size:12.5px;border-top:1px solid var(--line);padding-top:var(--s3)}

/* ---- sidebar dalam proyek ---- */
.kembali{display:block;padding:8px var(--s3);color:var(--dim);text-decoration:none;font-size:13px;border-radius:8px;margin-bottom:var(--s2)}
.kembali:hover{background:var(--panel2);color:var(--text)}
.navproj{margin:0 0 var(--s4);padding:var(--s3);border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.navproj small{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--faint)}
.navproj .nm{display:block;font-size:14px;font-weight:600;margin:3px 0 7px;word-break:break-word}

/* ---- wizard ---- */
.wizard{max-width:780px}
.langkah{display:flex;gap:var(--s2);margin-bottom:var(--s5)}
.langkah div{flex:1;padding:10px 12px;border-radius:8px;background:var(--panel);border:1px solid var(--line);
 font-size:13px;color:var(--faint)}
.langkah div.on{border-color:var(--accent);background:#0d2340;color:#79b8ff}
.langkah div.ok{border-color:#1f4a2c;color:var(--ok)}
.lbl{display:block;font-size:12.5px;color:var(--dim);margin:14px 0 6px}
.lbl:first-of-type{margin-top:0}

/* ---- lain-lain ---- */
.banner{border:1px solid #1d4a86;background:#0b1a2e;color:#9cc7ff;border-radius:8px;
 padding:10px var(--s4);margin-bottom:var(--s4);font-size:13px}
.banner a{color:#cfe3ff}
details.lanjut summary{cursor:pointer;color:var(--dim);font-size:13px;font-weight:500}
details.lanjut[open] summary{margin-bottom:var(--s3)}
.toast{position:fixed;right:24px;bottom:24px;max-width:420px;background:var(--panel2);border:1px solid var(--line);
 border-radius:10px;padding:12px 16px;font-size:13px;box-shadow:0 8px 30px #0008;opacity:0;transform:translateY(8px);
 transition:.2s;pointer-events:none}
.toast.on{opacity:1;transform:none}
.toast.ok{border-color:#1f4a2c} .toast.bad{border-color:#5c2b2b}
.kosong-besar{text-align:center;color:var(--dim);padding:var(--s6) var(--s4)}
</style>
<body><div class="app">
<aside>
  <div class="brand"><i></i> AI Office</div>
  <div id="sidebar"></div>
  <div class="sidefoot"><span id="now">—</span><a href="/logout">Keluar</a></div>
</aside>

<main>
  <div class="banner" id="banner" hidden></div>
  <div class="top">
    <h1 id="judul">Proyek</h1>
    <span class="sub" id="subjudul"></span>
    <div class="chips" id="chips"></div>
  </div>

  <!-- ===================== BERANDA: DAFTAR PROYEK ===================== -->
  <section class="page" id="p-home">
    <div class="proyek-grid" id="daftarProyek"></div>
  </section>

  <!-- ===================== PROYEK BARU ===================== -->
  <section class="page wizard" id="p-baru">
    <div class="langkah" id="langkah"></div>

    <div class="card" id="wz1">
      <h2>Nama dan ide</h2>
      <p class="hint">Pipeline belum dijalankan di langkah ini.</p>
      <label class="lbl" for="newname">Nama proyek</label>
      <input id="newname" placeholder="mis. sistem-cuti" autocomplete="off">
      <p class="hint" style="margin:6px 0 0">Dipakai sebagai nama folder — huruf, angka, tanda - dan _.</p>
      <label class="lbl" for="newidea">Ide aplikasi</label>
      <textarea id="newidea" rows="3" placeholder="Satu-dua kalimat. Kalau Anda sudah punya BRD atau FSD, cukup ringkasannya — dokumennya diunggah di langkah berikutnya."></textarea>
      <div class="row" style="margin-top:18px">
        <button onclick="wzBuat()">Lanjut ke bahan acuan</button>
        <button class="ghost" onclick="location.hash='#/'">Batal</button>
      </div>
      <div class="msg" id="wz1msg"></div>
    </div>

    <div id="wz2" hidden>
      <div class="card">
        <h2>Bahan acuan — <span id="wzNama"></span></h2>
        <p class="hint">Opsional. BRD, FSD, contoh desain, tangkapan layar, contoh data. Setiap peran membacanya
          sebagai acuan kuat, tapi tetap mengerjakan tahapnya sendiri. Dokumen Word simpan dulu sebagai PDF.</p>
        <div class="drop" id="wzDrop">
          <b>Jatuhkan berkas di sini</b>
          <small>atau klik untuk memilih — .md .txt .pdf .json .csv .png .jpg .webp .gif, maks 10 MB</small>
        </div>
        <input type="file" id="wzFile" multiple hidden>
        <div class="files" id="wzList"></div>
        <div class="msg" id="wzMsg"></div>
      </div>
      <div class="card">
        <h2>Mulai</h2>
        <p class="hint">Pipeline berjalan penuh dari Business Analyst sampai QA, dengan semua gate persetujuan
          lewat Telegram. Bahan yang diunggah setelah mulai tidak dibaca tahap yang sudah lewat.</p>
        <div class="row">
          <button id="wzMulai" onclick="mulai(wzProj)">Mulai pipeline</button>
          <button class="ghost" onclick="location.hash='#/p/'+wzProj+'/ringkasan'">Simpan, mulai nanti</button>
        </div>
        <div class="msg" id="wzMulaiMsg"></div>
      </div>
    </div>
  </section>

  <!-- ===================== DALAM PROYEK ===================== -->
  <section class="page" id="p-ringkasan">
    <div class="aksi" id="aksi"></div>
    <div class="card"><h2>Tahapan</h2><div class="steps" id="stages"></div></div>
    <div class="grid2">
      <div class="card"><h2>Hasil pengujian</h2><div class="kv" id="verdict"></div></div>
      <div class="card"><h2>Kuota langganan</h2>
        <div class="bar" id="qbar"><i></i></div><div class="kv" id="quota"></div></div>
    </div>
    <div class="card"><h2>Biaya per tahap</h2>
      <p class="hint">Tarif API setara; pemakaian langganan dipotong dari kuota, bukan ditagih.</p>
      <div class="kv" id="cost"></div></div>
    <details class="card lanjut">
      <summary>Pengaturan lanjutan</summary>
      <p class="hint">Mengulang dari tahap tertentu memotong proses: tahap sebelumnya dilewati dan dokumennya
        dipakai apa adanya. Untuk pemakaian biasa, cukup tombol di panel atas.</p>
      <div class="row">
        <select id="stage" style="width:auto">
          <option value="">pilih tahap…</option><option>ba</option><option>brd</option><option>fsd</option>
          <option>sa</option><option>design</option><option>dev</option><option>qa</option>
        </select>
        <button class="ghost" onclick="ulangDari()">Ulang dari tahap itu</button>
        <button class="ghost" id="unlock" hidden onclick="hapusKunci()">Hapus kunci basi</button>
      </div>
    </details>
  </section>

  <section class="page" id="p-acuan">
    <div class="card">
      <h2>Bahan acuan</h2>
      <p class="hint">Dibaca setiap peran sebagai acuan kuat. Kalau bertentangan dengan bagian lain, peran itu
        mengikuti bahan Anda lalu menulis keberatannya di KESENJANGAN supaya muncul di gate.</p>
      <div class="drop" id="drop">
        <b>Jatuhkan berkas di sini</b>
        <small>atau klik untuk memilih — .md .txt .pdf .json .csv .png .jpg .webp .gif, maks 10 MB</small>
      </div>
      <input type="file" id="berkas" multiple hidden>
      <div class="files" id="reflist"></div>
      <div class="msg" id="refmsg"></div>
    </div>
  </section>

  <section class="page" id="p-dokumen">
    <div class="card"><div class="docwrap">
      <div class="doclist" id="tabs"></div>
      <div class="doc mono" id="doc">(pilih dokumen)</div>
    </div></div>
  </section>

  <section class="page" id="p-tiket">
    <div class="card"><h2>Tiket dari TASKS.md</h2><div id="tasks"></div></div>
  </section>

  <section class="page" id="p-aktivitas">
    <div class="card"><h2>Log agent</h2>
      <p class="hint">Diperbarui otomatis. Menggulung sendiri selama Anda berada di bagian bawah.</p>
      <div class="log mono" id="log"></div></div>
  </section>

  <section class="page" id="p-perubahan">
    <div class="grid2">
      <div class="card">
        <h2>Lapor bug</h2>
        <p class="hint">Dokumen tidak diubah. Lead memperbaiki, lalu QA membuat skenario yang mereproduksi laporan Anda.</p>
        <textarea id="txtbug" rows="7" placeholder="Apa yang salah, langkah mengulanginya, dan apa yang seharusnya terjadi…"></textarea>
        <div class="row" style="margin-top:12px"><button class="kirim" onclick="kirimUbah('bug','txtbug')">Kirim laporan bug</button></div>
      </div>
      <div class="card">
        <h2>Minta fitur</h2>
        <p class="hint">Seluruh rantai dokumen diperbarui secara incremental dan ditandai [v1.1], lalu dikerjakan dan diuji.</p>
        <textarea id="txtubah" rows="7" placeholder="Fitur atau perubahan proses bisnis yang diinginkan…"></textarea>
        <div class="row" style="margin-top:12px"><button class="kirim" onclick="kirimUbah('change','txtubah')">Kirim permintaan</button></div>
      </div>
    </div>
    <div class="msg" id="ubahmsg"></div>
  </section>
</main></div>
<div class="toast" id="toast"></div>
<script>
const STAGES=[["ba","Business Analyst","PRD"],["brd","SA + Techwriter","BRD"],["fsd","SA + Techwriter","FSD"],["sa","System Analyst","SPEC"],["design","Product Designer","DESIGN"],["dev","Lead + Programmer","kode"],["qa","QA + Pentest","laporan"],["done","Selesai",""]];
const NAMA_TAHAP={ba:"PRD",brd:"BRD",fsd:"FSD",sa:"SPEC",design:"DESIGN",dev:"pengembangan",qa:"pengujian",done:"selesai"};
const LABEL={baru:"Belum dimulai",berjalan:"Berjalan",gate:"Menunggu keputusan",berhenti:"Berhenti",selesai:"Selesai"};
const HAL={ringkasan:"Ringkasan",acuan:"Bahan acuan",dokumen:"Dokumen",tiket:"Tiket",aktivitas:"Aktivitas",perubahan:"Bug & fitur"};
let proyek=[],jalan=null,aktif=null,hal="home",wzProj=null,offset=0,curDoc=null,stAktif={};

const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const escA=s=>esc(s).replace(/"/g,"&quot;").replace(/'/g,"&#39;");
const jam=t=>new Date(t*1000).toLocaleTimeString("id-ID",{hour:"2-digit",minute:"2-digit",second:"2-digit"});
function lalu(t){if(!t)return"";const d=Date.now()/1000-t;
 if(d<60)return"baru saja";if(d<3600)return Math.floor(d/60)+" menit lalu";
 if(d<86400)return Math.floor(d/3600)+" jam lalu";return Math.floor(d/86400)+" hari lalu"}
async function j(u){const r=await fetch(u);if(r.status===401){location.href="/login";return{}}return r.json()}
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
 if(r.status===401){location.href="/login";return{}}return r.json()}
let _tt;function toast(msg,ok){const t=$("#toast");t.textContent=msg;t.className="toast on "+(ok?"ok":"bad");
 clearTimeout(_tt);_tt=setTimeout(()=>t.className="toast",4200)}

/* ---------------- rute ---------------- */
function rute(){
 const h=location.hash.replace(/^#\/?/,"").split("/").filter(Boolean);
 if(h[0]==="p"&&h[1]){
  if(aktif!==h[1]){aktif=h[1];offset=0;curDoc=null;$("#log").innerHTML="";stAktif={}}
  hal=HAL[h[2]]?h[2]:"ringkasan";
 }else if(h[0]==="baru"){hal="baru";wzProj=h[1]||null;aktif=null}
 else{hal="home";aktif=null}
 document.querySelectorAll(".page").forEach(p=>p.classList.toggle("on",p.id==="p-"+hal));
 if(hal==="baru")renderWizard();
 tick();
}
addEventListener("hashchange",rute);

/* ---------------- kerangka ---------------- */
function renderSidebar(){
 const p=proyek.find(x=>x.name===aktif);
 if(!aktif){
  $("#sidebar").innerHTML=`<nav>
   <a href="#/" class="${hal==="home"?"on":""}">Semua proyek <span class="tag">${proyek.length}</span></a>
   <a href="#/baru" class="${hal==="baru"?"on":""}">+ Proyek baru</a></nav>`;
  return}
 const n=k=>`<a href="#/p/${aktif}/${k}" class="${hal===k?"on":""}">${HAL[k]}${
   k==="acuan"&&p?` <span class="tag">${p.refs}</span>`:k==="tiket"?` <span class="tag">${(stAktif.tasks||[]).length}</span>`:""}</a>`;
 $("#sidebar").innerHTML=`<a class="kembali" href="#/">← Semua proyek</a>
  <div class="navproj"><small>Proyek aktif</small><span class="nm">${esc(aktif)}</span>
   ${p?`<span class="badge ${p.kondisi}">${LABEL[p.kondisi]}</span>`:""}</div>
  <nav>${["ringkasan","acuan","dokumen","tiket","aktivitas","perubahan"].map(n).join("")}</nav>`;
}
function renderJudul(){
 if(hal==="home"){$("#judul").textContent="Proyek";$("#subjudul").textContent="pilih proyek untuk dibuka, atau buat yang baru";$("#chips").innerHTML="";return}
 if(hal==="baru"){$("#judul").textContent="Proyek baru";$("#subjudul").textContent="tiga langkah";$("#chips").innerHTML="";return}
 const p=proyek.find(x=>x.name===aktif);
 $("#judul").textContent=HAL[hal];$("#subjudul").textContent=aktif;
 $("#chips").innerHTML=p?`<span class="badge ${p.kondisi}">${LABEL[p.kondisi]}${p.stage&&p.kondisi!=="selesai"?" · "+esc(NAMA_TAHAP[p.stage]||p.stage):""}</span>`:"";
}
function renderBanner(){
 const b=$("#banner");
 if(jalan&&jalan.project!==aktif){
  b.hidden=false;
  b.innerHTML=`Pipeline <b>${esc(jalan.project)}</b> sedang berjalan. Hanya satu pipeline boleh jalan pada satu waktu — <a href="#/p/${escA(jalan.project)}/aktivitas">lihat</a>`;
 }else b.hidden=true;
}

/* ---------------- beranda ---------------- */
function renderHome(){
 const tambah=`<a class="pk tambah" href="#/baru"><span class="plus">+</span>
  <span class="nama">Proyek baru</span><span class="ide" style="flex:0">Beri nama dan ide, unggah BRD/FSD bila ada, lalu mulai.</span></a>`;
 const kartu=proyek.map(p=>{
  const tahap=p.stage&&p.kondisi!=="selesai"?`<span>tahap ${esc(NAMA_TAHAP[p.stage]||p.stage)}</span>`:"";
  const v=p.verdict&&p.kondisi==="selesai"?`<span>QA ${esc(p.verdict.qa)} · Pentest ${esc(p.verdict.pentest)}</span>`:"";
  return `<a class="pk" href="#/p/${escA(p.name)}/ringkasan">
   <div class="baris"><span class="badge ${p.kondisi}">${LABEL[p.kondisi]}</span>${tahap}${v}</div>
   <span class="nama">${esc(p.name)}</span>
   <span class="ide">${esc(p.idea||"(tanpa deskripsi ide)")}</span>
   <div class="baris">${p.refs?`<span>${p.refs} bahan acuan</span>`:""}${p.cost?`<span>$${p.cost.toFixed(2)}</span>`:""}<span class="kanan">${lalu(p.updated)}</span></div>
  </a>`}).join("");
 $("#daftarProyek").innerHTML=tambah+kartu;
}

/* ---------------- wizard ---------------- */
function renderWizard(){
 const s=wzProj?2:1;
 $("#langkah").innerHTML=["1. Nama & ide","2. Bahan acuan","3. Mulai"].map((x,i)=>
  `<div class="${i+1<s?"ok":i+1===s?"on":""}">${x}</div>`).join("");
 $("#wz1").hidden=s!==1;$("#wz2").hidden=s!==2;
 if(s===2){$("#wzNama").textContent=wzProj;muatRefs(wzProj,"wzList")}
}
async function wzBuat(){
 const n=$("#newname").value.trim(),i=$("#newidea").value.trim(),m=$("#wz1msg");
 if(!n||!i){m.className="msg bad";m.textContent="Nama dan ide harus diisi.";return}
 if(proyek.some(p=>p.name.toLowerCase()===n.toLowerCase())){m.className="msg bad";m.textContent=`Nama "${n}" sudah dipakai. Pilih nama lain, atau buka proyek itu dari daftar.`;return}
 m.className="msg wait";m.textContent="menyiapkan…";
 const r=await post("/api/create-project",{project:n,text:i});
 if(!r.ok){m.className="msg bad";m.textContent=r.msg;return}
 $("#newname").value="";$("#newidea").value="";m.textContent="";
 location.hash=`#/baru/${n}`;
}

/* ---------------- dalam proyek ---------------- */
function renderAksi(p,st){
 const lain=jalan&&jalan.project!==p.name;
 const nonaktif=lain?" disabled":"";
 const el=$("#aksi");el.className="aksi "+p.kondisi;
 let jd="",ket="",kecil="",tombol="";
 if(p.kondisi==="baru"){
  jd="Belum dimulai";
  ket=p.refs?`${p.refs} bahan acuan siap dibaca.`:"Belum ada bahan acuan. Kalau punya BRD atau FSD, unggah dulu.";
  kecil="Pipeline berjalan dari Business Analyst sampai QA, dengan persetujuan Anda di setiap gate.";
  tombol=`<button class="ghost" onclick="location.hash='#/p/${p.name}/acuan'">Bahan acuan</button><button${nonaktif} onclick="mulai('${p.name}')">Mulai pipeline</button>`;
 }else if(p.kondisi==="gate"){
  jd="Menunggu keputusan Anda";ket=st.gate?st.gate.question:"";
  kecil="Balas lewat Telegram: y (setuju) · q (berhenti) · atau tulis feedback untuk revisi.";
  tombol=`<button class="danger" onclick="hentikan()">Hentikan</button>`;
 }else if(p.kondisi==="berjalan"){
  jd=`Sedang mengerjakan ${NAMA_TAHAP[p.stage]||p.stage||"…"}`;ket=st.current||"Agent sedang bekerja.";
  tombol=`<button class="ghost" onclick="location.hash='#/p/${p.name}/aktivitas'">Lihat aktivitas</button><button class="danger" onclick="hentikan()">Hentikan</button>`;
 }else if(p.kondisi==="berhenti"){
  jd=`Berhenti di tahap ${NAMA_TAHAP[p.stage]||p.stage}`;
  ket=st.gate?`Terakhir menunggu persetujuan ${NAMA_TAHAP[p.stage]||p.stage}. Melanjutkan akan menanyakannya lagi lewat Telegram — dokumennya tidak dibuat ulang.`
   :"Proses tidak sedang berjalan. Melanjutkan akan meneruskan dari tahap ini.";
  tombol=`<button${nonaktif} onclick="lanjutkan('${p.name}')">Lanjutkan</button>`;
 }else if(p.kondisi==="selesai"){
  const v=st.verdict||{};const buka=v.qa==="FAIL"||v.pentest==="FAIL";
  jd=buka?"Selesai — ada temuan yang belum diperbaiki":"Selesai";
  ket=`QA ${v.qa||"—"} · Pentest ${v.pentest||"—"}`;
  kecil="Menemukan bug saat memakai aplikasinya, atau butuh fitur tambahan?";
  tombol=`<button${nonaktif} onclick="location.hash='#/p/${p.name}/perubahan'">Lapor bug / minta fitur</button>`;
 }
 const catatan=lain&&p.kondisi!=="gate"&&p.kondisi!=="berjalan"
  ?`<div class="catatan">Tombol nonaktif: pipeline <b>${esc(jalan.project)}</b> sedang berjalan.</div>`:"";
 el.innerHTML=`<div class="teks"><span class="jd">${esc(jd)}</span><span class="ket">${esc(ket)}</span>${kecil?`<span class="kecil">${esc(kecil)}</span>`:""}</div><div class="row">${tombol}</div>${catatan}`;
 $("#unlock").hidden=!jalan;
 document.querySelectorAll("#p-perubahan .kirim").forEach(b=>b.disabled=!!jalan);
}
const DOK_TAHAP={ba:"PRD",brd:"BRD",fsd:"FSD",sa:"SPEC",design:"DESIGN",dev:"TASKS",qa:"QA_REPORT"};
function renderStages(st){
 const idx=STAGES.findIndex(s=>s[0]===st.stage);const ada=st.docs||[];
 $("#stages").innerHTML=STAGES.map((s,i)=>{
  // Tahap yang sudah lewat tapi dokumennya tidak ada berarti dilewati, bukan selesai.
  const lewat=i<idx||(i===idx&&st.stage==="done");
  const c=lewat?(s[0]==="done"||!DOK_TAHAP[s[0]]||ada.includes(DOK_TAHAP[s[0]])?"done":"skip")
   :(i===idx?"live":"");
  return `<div class="step ${c}"><b>${s[1]}</b><small>${s[2]||"—"}</small></div>`}).join("");
}
function renderVerdict(st){
 const v=st.verdict||{};const w=x=>x==="PASS"?`<span style="color:var(--ok)">PASS</span>`:x==="FAIL"?`<span style="color:var(--bad)">FAIL</span>`:"—";
 $("#verdict").innerHTML=`<span>Iterasi</span><span>${v.iteration??"—"}</span><span>QA</span><span>${w(v.qa)}</span><span>Pentest</span><span>${w(v.pentest)}</span>`;
}
function renderQuota(st){
 const q=st.quota||{};const u=q.utilization!=null?Math.round(q.utilization*100):null;
 const reset=q.resets_at?new Date(q.resets_at>1e12?q.resets_at:q.resets_at*1000).toLocaleString("id-ID",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}):"—";
 const bar=$("#qbar");bar.className="bar"+(u>=90?" bad":u>=70?" warn":"");bar.firstElementChild.style.width=(u||0)+"%";
 $("#quota").innerHTML=`<span>Jenis</span><span>${esc(q.type||"—")}</span><span>Status</span><span>${esc(q.status||"—")}</span><span>Terpakai</span><span>${u==null?"— (dikirim server saat mendekati batas)":u+"%"}</span><span>Reset</span><span>${reset}</span>`;
 const c=st.cost||{};const baris=Object.entries(c).filter(([k])=>k!=="total");
 $("#cost").innerHTML=baris.length?baris.map(([k,v])=>`<span>${esc(k)}</span><span>$${v.toFixed(2)}</span>`).join("")+`<span><b>Total</b></span><span><b>$${(c.total||0).toFixed(2)}</b></span>`
  :`<span>Belum ada tahap yang selesai</span><span>—</span>`;
}
function renderTasks(st){
 const t=st.tasks||[];
 if(!t.length){$("#tasks").innerHTML=`<div class="kosong">Belum ada tiket. Tiket muncul setelah tahap pengembangan dimulai.</div>`;return}
 const n=s=>t.filter(x=>x.status===s).length;
 $("#tasks").innerHTML=`<p class="hint">${n("DONE")} selesai · ${n("IN_PROGRESS")} dikerjakan · ${n("TODO")} antre · ${n("BLOCKED")} terhambat</p>
  <table><tr><th>ID</th><th>Tiket</th><th>Status</th></tr>${t.map(x=>`<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.title)}<br><span style="color:var(--faint);font-size:12px">${esc(x.file)}</span></td><td><span class="st ${escA(x.status)}">${esc(x.status)}</span></td></tr>`).join("")}</table>`;
}
function renderTabs(st){
 const d=st.docs||[];const urut=["QA_REPORT","PENTEST_REPORT","FIXES","TASKS","DESIGN","SPEC","FSD","BRD","PRD","IDEA"];
 const ds=urut.filter(x=>d.includes(x));
 $("#tabs").innerHTML=ds.length?ds.map(x=>`<button class="${x===curDoc?"on":""}" onclick="bukaDok('${x}')">${x.replace(/_/g," ")}</button>`).join("")
  :`<div class="kosong">Belum ada dokumen.</div>`;
 if(!curDoc&&ds.length&&hal==="dokumen")bukaDok(ds[0]);
}
async function bukaDok(n){curDoc=n;const d=await j(`/api/doc?project=${aktif}&name=${n}`);$("#doc").textContent=d.text||"(kosong)";
 document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.textContent===n.replace(/_/g," ")))}
function baris(e){let s="";
 switch(e.kind){
  case"stage_start":s=`▶ ${e.label} mulai (${e.model}${e.resumed?", resume":""})`;break;
  case"model":s=`${e.label}: model ${e.alias} = ${e.model}`;break;
  case"stage_end":s=`${e.ok?"✔":"✖"} ${e.label} selesai — ${e.turns} turn, $${(e.cost||0).toFixed(2)}`;break;
  case"agent":s=`[${e.label}] ${e.text}`;break;
  case"subagent":s=`[${e.label}] → ${e.agent}: ${e.desc}`;break;
  case"tool":s=`[${e.label}] ${e.tool} ${e.target||""}`;break;
  case"gate":s=`⏸ ${e.label}: ${e.question}`;break;
  case"gate_answer":s=`▶ jawaban (${e.source}): ${e.answer}`;break;
  case"quota":s=`⚠ kuota ${e.type}: ${e.status}`;break;
  case"verdict":s=`iterasi ${e.iteration}: QA=${e.qa} Pentest=${e.pentest}`;break;
  case"error":s=`✖ ${e.label}: ${e.error}`;break;
  default:s=e.msg||JSON.stringify(e)}
 return `<div class="k-${escA(e.kind)}"><span class="t">${jam(e.t)}</span>${esc(s)}</div>`;
}
async function renderProyek(){
 const p=proyek.find(x=>x.name===aktif);
 if(!p){$("#aksi").className="aksi";$("#aksi").innerHTML=`<div class="teks"><span class="jd">Proyek tidak ditemukan</span><span class="ket">Mungkin sudah dihapus. Kembali ke daftar proyek.</span></div>`;return}
 const st=await j(`/api/status?project=${aktif}`);stAktif=st||{};
 renderAksi(p,stAktif);renderStages({...stAktif,stage:p.stage});renderVerdict(stAktif);renderQuota(stAktif);renderTasks(stAktif);renderTabs(stAktif);
 if(hal==="acuan")muatRefs(aktif,"reflist");
 const ev=await j(`/api/events?project=${aktif}&offset=${offset}`);
 if(ev.events&&ev.events.length){const log=$("#log");const bawah=log.scrollTop+log.clientHeight>=log.scrollHeight-40;
  log.insertAdjacentHTML("beforeend",ev.events.map(baris).join(""));if(bawah)log.scrollTop=log.scrollHeight;
  if(curDoc&&ev.events.some(e=>e.kind==="stage_end"))bukaDok(curDoc)}
 offset=ev.offset||offset;
}

/* ---------------- aksi ---------------- */
async function mulai(p){
 if(!p)return;
 if(!confirm("Mulai pipeline sekarang?\n\nPastikan semua bahan acuan sudah diunggah — bahan yang ditambahkan belakangan tidak dibaca tahap yang sudah lewat."))return;
 const r=await post("/api/run",{project:p,action:"lanjut",text:""});
 toast(r.ok?"Pipeline dimulai.":r.msg,r.ok);
 if(r.ok)location.hash=`#/p/${p}/ringkasan`;else tick();
}
async function lanjutkan(p){const r=await post("/api/run",{project:p,action:"lanjut",text:""});toast(r.ok?"Pipeline dilanjutkan.":r.msg,r.ok);tick()}
async function hentikan(){if(!confirm("Hentikan pipeline yang sedang berjalan?"))return;const r=await post("/api/stop");toast(r.msg,r.ok);tick()}
async function ulangDari(){
 const t=$("#stage").value;if(!t){toast("Pilih tahapnya dulu.",false);return}
 if(!confirm(`Ulang dari tahap ${t}? Tahap sebelumnya dilewati dan dokumennya dipakai apa adanya.`))return;
 const r=await post("/api/run",{project:aktif,action:"resume",text:t});toast(r.ok?`Dimulai dari tahap ${t}.`:r.msg,r.ok);tick()}
async function hapusKunci(){
 if(!confirm("Hapus kunci? Lakukan HANYA kalau pipeline-nya memang sudah mati. Kalau masih hidup, dua pipeline bisa jalan bersamaan dan saling menimpa berkas."))return;
 const r=await post("/api/clear-lock");toast(r.msg,r.ok);tick()}
async function kirimUbah(aksi,id){
 const teks=$("#"+id).value.trim();if(!teks){toast("Teksnya masih kosong.",false);return}
 const r=await post("/api/run",{project:aktif,action:aksi,text:teks});
 toast(r.ok?(aksi==="bug"?"Laporan bug dikirim, pipeline perbaikan dimulai.":"Permintaan fitur dikirim, pipeline dimulai."):r.msg,r.ok);
 if(r.ok){$("#"+id).value="";location.hash=`#/p/${aktif}/ringkasan`}}

/* ---------------- unggah ---------------- */
function pasangUnggah(dropId,inputId,listId,msgId,ambilProj){
 const d=$("#"+dropId),inp=$("#"+inputId);
 d.onclick=()=>inp.click();
 inp.onchange=e=>kirim(e.target.files);
 ["dragenter","dragover"].forEach(n=>d.addEventListener(n,e=>{e.preventDefault();d.classList.add("over")}));
 ["dragleave","drop"].forEach(n=>d.addEventListener(n,e=>{e.preventDefault();d.classList.remove("over")}));
 d.addEventListener("drop",e=>kirim(e.dataTransfer.files));
 async function kirim(files){
  const p=ambilProj(),m=$("#"+msgId);
  if(!p||!files||!files.length)return;
  m.className="msg wait";m.textContent=`mengunggah ${files.length} berkas…`;
  const gagal=[];
  for(const f of files){
   const b64=await new Promise(r=>{const fr=new FileReader();fr.onload=()=>r(fr.result.split(",")[1]);fr.readAsDataURL(f)});
   const res=await post("/api/upload",{project:p,nama:f.name,b64});if(!res.ok)gagal.push(`${f.name}: ${res.msg}`)}
  m.className="msg "+(gagal.length?"bad":"ok");
  m.textContent=gagal.length?gagal.join(" | "):`${files.length} berkas tersimpan.`;
  inp.value="";muatRefs(p,listId);tick();
 }
}
async function muatRefs(p,listId){
 if(!p)return;const r=(await j(`/api/refs?project=${p}`)).refs||[];
 $("#"+listId).innerHTML=r.length?r.map(x=>`<div class="file"><span class="nm mono">${esc(x.nama)}</span>
  <span class="sz">${Math.max(1,Math.round(x.byte/1024))} KB</span>
  <button title="hapus" data-n="${escA(x.nama)}" onclick="hapusRef('${p}',this.dataset.n,'${listId}')">×</button></div>`).join("")
  :`<div class="kosong">Belum ada bahan acuan.</div>`;
}
async function hapusRef(p,n,listId){if(!confirm(`Hapus "${n}"?`))return;const r=await post("/api/ref-delete",{project:p,nama:n});toast(r.msg,r.ok);muatRefs(p,listId);tick()}

/* ---------------- detak ---------------- */
let sibuk=false;
async function tick(){
 if(sibuk)return;sibuk=true;
 try{
  const [ps,rn]=await Promise.all([j("/api/projects"),j("/api/running")]);
  proyek=Array.isArray(ps)?ps:[];jalan=(rn&&rn.running)||null;
  if(aktif)await renderProyek();
  renderSidebar();renderJudul();renderBanner();
  if(hal==="home")renderHome();
  if(hal==="baru"){const b=$("#wzMulai");if(b)b.disabled=!!jalan}
  $("#now").textContent="diperbarui "+jam(Date.now()/1000);
 }catch(e){$("#now").textContent="koneksi terputus"}
 finally{sibuk=false}
}

pasangUnggah("wzDrop","wzFile","wzList","wzMsg",()=>wzProj);
pasangUnggah("drop","berkas","reflist","refmsg",()=>aktif);
rute();setInterval(tick,2500);
</script></body></html>"""

if __name__ == "__main__":
    if butuh_sandi() and not PASS:
        sys.exit(
            f"Menolak berjalan.\n"
            f"DASHBOARD_HOST={HOST} membuka dashboard ke jaringan, tapi DASHBOARD_PASS kosong.\n"
            f"Dashboard ini bisa menjalankan pipeline, dan agent di dalamnya punya akses Bash,\n"
            f"jadi tanpa sandi ini setara memberi shell ke siapa pun yang menjangkau port ini.\n\n"
            f"Isi DASHBOARD_PASS di .env, atau biarkan DASHBOARD_HOST=127.0.0.1 lalu akses\n"
            f"dari jauh lewat terowongan SSH:\n"
            f"  ssh -L {PORT}:127.0.0.1:{PORT} user@server")
    print(f"Dashboard: http://{HOST}:{PORT}  (Ctrl+C untuk berhenti)")
    if PASS:
        print(f"Login: pengguna '{USER}', sandi dari DASHBOARD_PASS.")
        if butuh_sandi():
            print("PERINGATAN: Basic auth lewat HTTP polos - sandi terkirim tanpa enkripsi.")
            print("Untuk dipakai sungguhan, taruh di belakang reverse proxy ber-TLS.")
    else:
        print("Tanpa sandi - hanya melayani 127.0.0.1 (mesin ini saja).")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
