"""AI Office — pipeline kantor software dengan karyawan AI.

BA -> SA+TW: BRD -> [gate] -> SA+TW: FSD -> [gate] -> SA: SPEC -> [gate]
   -> Lead/Programmer -> QA+Pentest -> loop perbaikan -> selesai

Pakai:
    python office.py "aplikasi pencatat cuti karyawan dengan approval atasan"
    python office.py --project <nama> --resume dev
    python office.py --project <nama> --change "tambah fitur export laporan cuti ke Excel"
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
import time
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

import monitor
import roles

# Konsol Windows default cp1252, sedangkan keluaran agent rutin memuat panah,
# em-dash, dan centang. Tanpa ini satu karakter saja menjatuhkan pipeline
# di tengah tahap lewat UnicodeEncodeError.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()
# ANTHROPIC_API_KEY kosong = pakai login langganan Claude Code (OAuth).
if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.pop("ANTHROPIC_API_KEY", None)

ROOT = Path(__file__).parent
STAGES = ["ba", "brd", "fsd", "sa", "design", "dev", "qa"]


LOCK = ROOT / ".pipeline.lock"


def lock_acquire(project: str, action: str):
    """Umumkan pipeline ini sedang berjalan supaya dashboard tahu dan tidak
    menjalankan yang kedua. Dibersihkan di finally pada main()."""
    LOCK.write_text(json.dumps({"pid": os.getpid(), "project": project,
                                "action": action, "started": time.time()}), encoding="utf-8")


def lock_release():
    try:
        if LOCK.exists() and json.loads(LOCK.read_text()).get("pid") == os.getpid():
            LOCK.unlink()
    except Exception:
        pass


class StageFailed(Exception):
    """Tahap gagal (kuota habis, error API, budget tercapai). Pipeline harus berhenti."""


LAST_RATE_LIMIT: dict = {}   # info rate limit terakhir dari server


class Sessions:
    """Simpan session_id Claude Code per tahap di docs/SESSIONS.json supaya bisa di-resume."""

    def __init__(self, docs: Path):
        self.path = docs / "SESSIONS.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, label): return self.data.get(label)

    def set(self, label, sid):
        if sid and self.data.get(label) != sid:
            self.data[label] = sid
            self.path.write_text(json.dumps(self.data, indent=2))

    def clear(self, label):
        if label in self.data:
            del self.data[label]
            self.path.write_text(json.dumps(self.data, indent=2))


SESSIONS: Sessions | None = None


def note_rate_limit(ev: RateLimitEvent):
    info = ev.rate_limit_info
    LAST_RATE_LIMIT.update(
        status=info.status, resets_at=info.resets_at,
        type=info.rate_limit_type, utilization=info.utilization,
    )
    monitor.set_quota(status=info.status, type=info.rate_limit_type,
                      utilization=info.utilization, resets_at=info.resets_at)
    if info.status in ("allowed_warning", "rejected"):
        monitor.emit("quota", status=info.status, type=info.rate_limit_type,
                     utilization=info.utilization, resets_at=info.resets_at)
        pct = f"{info.utilization*100:.0f}%" if info.utilization is not None else "?"
        print(f"  !! kuota {info.rate_limit_type}: {info.status} (terpakai {pct}), "
              f"reset {fmt_reset(info.resets_at)}")


def reset_epoch() -> float | None:
    r = LAST_RATE_LIMIT.get("resets_at")
    if not r:
        return None
    return r / 1000 if r > 1e12 else r      # server kadang kirim ms, kadang detik


def fmt_reset(r) -> str:
    if not r:
        return "(tidak diketahui)"
    e = r / 1000 if r > 1e12 else r
    return datetime.fromtimestamp(e).strftime("%d %b %H:%M")


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------
def budget(name: str, default: float) -> float:
    return float(os.getenv(f"BUDGET_{name}", default))


def penyedia() -> str:
    """claude | openrouter | custom. USE_OPENROUTER=1 lama tetap dihormati."""
    p = os.getenv("AI_PROVIDER", "").strip().lower()
    if not p and os.getenv("USE_OPENROUTER") == "1":
        return "openrouter"
    return p or "claude"


def provider_env() -> dict:
    """Variabel lingkungan untuk Claude Code supaya memanggil host lain.

    Claude Code hanya berbicara skema Anthropic (/v1/messages), jadi host kustom
    WAJIB melayani skema itu - endpoint yang cuma OpenAI (/v1/chat/completions)
    tidak bisa dipakai lewat jalur ini.
    """
    p = penyedia()
    if p == "openrouter":
        return {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN": os.environ["OPENROUTER_API_KEY"],
            "ANTHROPIC_API_KEY": "",
        }
    if p == "custom":
        base = os.environ["CUSTOM_BASE_URL"].strip().rstrip("/")
        # Claude Code menambahkan /v1/messages sendiri; buang /v1 kalau ikut ditulis.
        if base.endswith("/v1"):
            base = base[:-3]
        model = os.environ["CUSTOM_MODEL"].strip()
        return {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": os.environ["CUSTOM_API_KEY"].strip(),
            "ANTHROPIC_API_KEY": "",
            # Peran memakai alias opus/sonnet/haiku; petakan ke nama model di host.
            # Masing-masing bisa ditimpa, mis. CUSTOM_MODEL_OPUS=Claude-Opus-5.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": os.getenv("CUSTOM_MODEL_OPUS", "").strip() or model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": os.getenv("CUSTOM_MODEL_SONNET", "").strip() or model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.getenv("CUSTOM_MODEL_HAIKU", "").strip() or model,
            "ANTHROPIC_SMALL_FAST_MODEL": os.getenv("CUSTOM_MODEL_HAIKU", "").strip() or model,
        }
    return {}


def auth_mode() -> str:
    p = penyedia()
    if p == "openrouter":
        return "OpenRouter"
    if p == "custom":
        return f"host kustom {os.getenv('CUSTOM_BASE_URL')} (model {os.getenv('CUSTOM_MODEL')})"
    return "API key" if os.environ.get("ANTHROPIC_API_KEY") else "langganan Claude (OAuth)"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def verdict(path: Path) -> str:
    if not path.exists():
        return "FAIL"
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        m = re.match(r"\s*VERDICT:\s*(PASS|FAIL)", line, re.I)
        if m:
            return m.group(1).upper()
    return "FAIL"


async def gate(question: str, label: str = "GATE", files: list | None = None) -> str:
    """files = dokumen yang dilampirkan ke Telegram supaya Bos bisa review dari HP."""
    ans = await monitor.ask(question, label, files)
    if ans.lower() == "q":
        monitor.emit("info", msg="Dihentikan oleh user")
        monitor.tg_send("⏹ Pipeline dihentikan oleh user.")
        print("Dihentikan oleh user.")
        sys.exit(0)
    return ans


# ---------------------------------------------------------------------------
# Eksekutor satu tahap
# ---------------------------------------------------------------------------
RESUME_NOTE = (
    "Kamu dimulai ulang setelah proses terputus (kuota habis / aplikasi mati). "
    "Beberapa langkah terakhirmu mungkin belum tersimpan. Cek dulu keadaan nyata di disk "
    "(file yang ada, docs/TASKS.md, hasil test), lalu LANJUTKAN tugas ini — jangan mulai dari nol: "
)


async def run_stage(label: str, prompt: str, role: dict, cwd: Path,
                    budget_usd: float, agents: dict | None = None) -> str:
    prev = SESSIONS.get(label) if SESSIONS else None
    if prev:
        print(f"\n### [{label}] RESUME sesi {prev[:8]}... — model={role['model']} budget=${budget_usd}")
        prompt = RESUME_NOTE + prompt
    else:
        print(f"\n### [{label}] mulai — model={role['model']} budget=${budget_usd}")
    monitor.emit("stage_start", label=label, model=role["model"], resumed=bool(prev))
    monitor.set_status(current=label, current_since=time.time())
    monitor.tg_send(f"▶ {label} mulai" + (" (resume)" if prev else ""))
    opts = ClaudeAgentOptions(
        resume=prev,
        cwd=str(cwd),
        model=role["model"],
        system_prompt=role.get("system_prompt"),
        allowed_tools=role["tools"],
        disallowed_tools=roles.DENY_BASH,
        permission_mode="acceptEdits",
        agents=agents,
        setting_sources=role.get("settings", []),
        max_budget_usd=budget_usd,
        env=provider_env(),
    )
    final = ""
    err_kind = None
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                # Alias "opus"/"sonnet" diterjemahkan CLI dan bisa berpindah saat
                # Claude Code diperbarui. Catat model SEBENARNYA supaya biaya dan
                # kualitas tiap tahap bisa dirunut ke model yang nyata dipakai.
                asli = msg.data.get("model")
                if asli and asli != role.get("model"):
                    print(f"  [{label}] model: {role.get('model')} -> {asli}")
                monitor.emit("model", label=label, alias=role.get("model"), model=asli)
                if SESSIONS:
                    SESSIONS.set(label, msg.data.get("session_id"))
        elif isinstance(msg, RateLimitEvent):
            note_rate_limit(msg)
        elif isinstance(msg, AssistantMessage):
            if msg.error:
                err_kind = msg.error
                print(f"  [{label}] !! error dari API: {msg.error}")
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    print(f"  [{label}] {b.text.strip()[:300]}")
                    monitor.emit("agent", label=label, text=b.text.strip()[:600])
                elif isinstance(b, ToolUseBlock) and b.name == "Agent":
                    desc = str(b.input.get("description", ""))[:120]
                    print(f"  [{label}] -> subagent {b.input.get('subagent_type')}: {desc}")
                    monitor.emit("subagent", label=label, agent=b.input.get("subagent_type"), desc=desc)
                elif isinstance(b, ToolUseBlock):
                    tgt = b.input.get("file_path") or b.input.get("command") or b.input.get("pattern") or ""
                    monitor.emit("tool", label=label, tool=b.name, target=str(tgt)[:160])
        elif isinstance(msg, ResultMessage):
            if SESSIONS:
                SESSIONS.set(label, msg.session_id)
            final = msg.result or ""
            print(f"### [{label}] selesai — turns={msg.num_turns} "
                  f"cost=${(msg.total_cost_usd or 0):.3f} subtype={msg.subtype}")
            monitor.add_cost(label, msg.total_cost_usd or 0)
            monitor.emit("stage_end", label=label, turns=msg.num_turns,
                         cost=msg.total_cost_usd, subtype=msg.subtype, ok=not msg.is_error)
            monitor.tg_send(f"{'✔' if not msg.is_error else '✖'} {label} selesai — "
                            f"{msg.num_turns} turn, ~${(msg.total_cost_usd or 0):.2f}")
            if msg.is_error or msg.subtype != "success":
                err_kind = err_kind or msg.subtype or "unknown"
                print(f"### [{label}] GAGAL: subtype={msg.subtype} "
                      f"http={msg.api_error_status} errors={msg.errors}")
    if err_kind:
        monitor.emit("error", label=label, error=err_kind)
        raise StageFailed(f"{label}: {err_kind}")
    if SESSIONS:
        SESSIONS.clear(label)      # tahap sukses -> sesi tidak perlu di-resume lagi
    return final


async def wait_for_quota(label: str) -> bool:
    """Tunggu sampai kuota reset menurut server. True = lanjut ulang, False = user minta berhenti."""
    mode = os.getenv("QUOTA_WAIT", "auto")          # auto | ask
    max_h = float(os.getenv("QUOTA_WAIT_MAX_HOURS", 6))
    reset = reset_epoch()
    secs = (reset - time.time() + 60) if reset else 0  # +1 menit jaga-jaga
    kind = LAST_RATE_LIMIT.get("type", "?")

    if secs <= 0:
        # tidak ada info reset (mis. rate limit sesaat) -> tunggu sebentar saja
        secs = 5 * 60
        print(f">>> Kuota/rate limit tanpa info reset. Menunggu 5 menit lalu coba lagi.")
    else:
        print(f">>> Kuota {kind} habis. Reset menurut server: {fmt_reset(LAST_RATE_LIMIT['resets_at'])} "
              f"(~{secs/3600:.1f} jam lagi).")

    if mode == "ask" or secs > max_h * 3600:
        ans = await gate(f"Tahap {label} berhenti karena kuota. 'y' = tunggu sampai reset lalu ulang otomatis, "
                   f"'q' = berhenti (lanjutkan nanti dengan --resume).")
        if ans.lower() != "y":
            return False

    until = datetime.fromtimestamp(time.time()+secs).strftime('%d %b %H:%M')
    print(f">>> Tidur sampai {until} ...")
    monitor.set_status(current=f"menunggu kuota s/d {until}")
    monitor.tg_send(f"💤 Kuota {kind} habis. Menunggu sampai {until}, lalu {label} diulang.")
    await asyncio.sleep(secs)
    return True


async def run_stage_retry(label, prompt, role, cwd, budget_usd, agents=None):
    """Bungkus run_stage: kalau kuota habis, tunggu sampai reset lalu ulang tahap yang sama."""
    while True:
        try:
            return await run_stage(label, prompt, role, cwd, budget_usd, agents)
        except StageFailed as e:
            print(f"\n>>> {e}")
            is_quota = "rate_limit" in str(e) or LAST_RATE_LIMIT.get("status") == "rejected"
            if not is_quota and SESSIONS and SESSIONS.get(label) and "invalid_request" in str(e):
                print(f">>> Sesi lama tidak bisa di-resume. Mulai sesi baru untuk {label}.")
                SESSIONS.clear(label)
                continue
            if is_quota:
                if await wait_for_quota(label):
                    continue
                raise
            # Gagal berulang sering berarti tahapnya terlalu berat untuk model itu,
            # bukan sekadar sial. Tawarkan naik satu tingkat, jangan mengulang
            # dengan model yang sama terus-menerus.
            naik = roles.naik_model(role.get("model", ""))
            tawaran = (f"'y' = ulang dengan model {naik} (naik dari {role['model']})"
                       if naik else
                       f"'y' = ulang dengan model {role.get('model')} (sudah tingkat tertinggi)")
            ans = await gate(f"Tahap {label} gagal ({e}). {tawaran}, "
                       f"'q' = berhenti (lanjutkan nanti dengan --resume).")
            if ans.lower() != "y":
                raise
            if naik:
                # Salin dict-nya: definisi peran di roles.py dipakai bersama tahap
                # lain, jangan sampai kenaikan ini bocor ke sana.
                role = {**role, "model": naik}
                print(f">>> {label} dinaikkan ke model {naik}.")
                monitor.emit("info", msg=f"{label} dinaikkan ke model {naik}")
                monitor.tg_send(f"⬆ {label} diulang dengan model {naik}.")


def kesenjangan(teks: str) -> str:
    """Ambil blok KESENJANGAN dari laporan akhir sebuah peran.

    Keluhan agent tidak berguna kalau terkubur di dokumen 70 KB — ini menaikkannya
    ke pertanyaan gate, supaya Bos melihatnya saat memutuskan, bukan setelahnya.
    """
    if not teks:
        return ""
    i = teks.upper().rfind("KESENJANGAN")
    if i < 0:
        return ""
    blok = teks[i:].strip()
    baris = [b.rstrip() for b in blok.splitlines()]
    isi = [b for b in baris[1:] if b.strip().startswith(("-", "*", "1", "2", "3", "4", "5"))]
    if not isi and "tidak ada" in baris[0].lower():
        return ""
    return "\n".join(isi[:5])


async def doc_with_gate(label: str, doc: str, prompt: str, role: dict, ws: Path,
                        budget_usd: float, agents=None):
    """Tulis dokumen -> gate review -> revisi sampai user setuju."""
    docs = ws / "docs"
    fb = docs / f"{doc}_FEEDBACK.md"
    # Penanda "dokumen sudah jadi, tinggal menunggu approval". Kalau proses mati
    # tepat di gate, restart langsung ke gate — tahap tidak digenerate ulang.
    pending = docs / f".{doc}_PENDING_GATE"
    while True:
        if pending.exists() and (docs / f"{doc}.md").exists():
            print(f">>> {doc}.md sudah dibuat sebelumnya — langsung ke gate, tidak digenerate ulang.")
        else:
            pending.unlink(missing_ok=True)
            laporan = await run_stage_retry(label, prompt, role, ws, budget_usd, agents)
            pending.write_text(laporan or "1", encoding="utf-8")
        gap = kesenjangan(pending.read_text(encoding="utf-8"))
        tanya = f"Review {docs / (doc + '.md')}. Setuju?"
        if gap:
            tanya += f"\n\nYang dikeluhkan {label}:\n{gap}"
        ans = await gate(tanya, files=[docs / f"{doc}.md"])
        pending.unlink(missing_ok=True)
        if ans.lower() == "y":
            fb.unlink(missing_ok=True)
            return
        fb.write_text(ans, encoding="utf-8")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def pipeline(idea: str, project: str, start: str, change: str | None,
                   bug: str | None = None):
    ws = ROOT / "workspace" / project
    docs, app = ws / "docs", ws / "app"
    docs.mkdir(parents=True, exist_ok=True)
    app.mkdir(exist_ok=True)
    cr = docs / "CHANGE_REQUEST.md"
    global SESSIONS
    SESSIONS = Sessions(docs)
    monitor.init(docs, project)
    monitor.emit("info", msg=f"pipeline mulai dari tahap {start}" + (f" (change: {change})" if change else ""))

    if change:
        # Jalur change request: semua tahap berjalan incremental
        cr.write_text(change, encoding="utf-8")
        start = "ba"
        print(f"\n>>> MODE CHANGE REQUEST: {change}")
    elif bug:
        # Jalur laporan bug: kebutuhan TIDAK berubah, jadi PRD/BRD/FSD/SPEC
        # tidak disentuh. Yang salah implementasinya, bukan spesifikasinya.
        cr.unlink(missing_ok=True)
        br = docs / "BUG_REPORT.md"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = "# Laporan bug dari pemilik produk\n\n"
        entry = f"## Laporan {stamp}\n\n{bug}\n"
        br.write_text((br.read_text(encoding="utf-8") + "\n") if br.exists() else header,
                      encoding="utf-8")
        with br.open("a", encoding="utf-8") as f:
            f.write(entry)
        fx = docs / "FIXES.md"
        with fx.open("a", encoding="utf-8") as f:
            f.write(f"\n\n## WAJIB - laporan bug pemilik produk ({stamp})\n\n{bug}\n")
        start = "qa"
        print(f"\n>>> MODE LAPORAN BUG: {bug}")
    else:
        cr.unlink(missing_ok=True)
        (docs / "IDEA.md").write_text(idea, encoding="utf-8")

    i = STAGES.index(start)

    # Masuk di tengah pipeline (--resume, atau dokumen yang diimpor Bos) hanya sah
    # kalau dokumen masukannya benar-benar ada. Tanpa ini agent jalan terus dan
    # mengarang isinya - kegagalan yang mahal dan sulit disadari.
    BUTUH = {"fsd": ["BRD.md"], "sa": ["FSD.md"], "design": ["SPEC.md"],
             "dev": ["SPEC.md"], "qa": ["SPEC.md"]}
    kurang = [f for f in BUTUH.get(start, []) if not (docs / f).exists()]
    if kurang:
        sys.exit(
            f"\nTidak bisa mulai dari tahap '{start}': {', '.join(kurang)} tidak ada di "
            f"{docs}.\n"
            f"Tahap itu membaca dokumen tersebut sebagai masukan.\n"
            f"  Punya dokumennya  : taruh di {docs} lalu ulangi perintah ini\n"
            f"  Belum punya       : mulai dari tahap lebih awal, mis. --resume ba")
    state = docs / "STATE.txt"

    def mark(stage):
        state.write_text(stage, encoding="utf-8")
        monitor.set_status(stage=stage)

    # 1. Business Analyst -> PRD
    if i <= 0:
        mark("ba")
        p = "Ide aplikasi:\n" + idea
        if change:
            p = "Permintaan perubahan ada di docs/CHANGE_REQUEST.md. Update docs/PRD.md secara incremental."
        await run_stage_retry("BA", p, roles.BA, ws, budget("BA", 1.0))

    # 2. SA + Technical Writer -> BRD [gate]
    if i <= 1:
        mark("brd")
        await doc_with_gate("BRD", "BRD", "Susun docs/BRD.md dari docs/PRD.md, lalu rapikan lewat techwriter.",
                            roles.BRD, ws, budget("DOC", 2.0), roles.SUBAGENTS)

    # 3. SA + Technical Writer -> FSD [gate]
    if i <= 2:
        mark("fsd")
        await doc_with_gate("FSD", "FSD", "Susun docs/FSD.md dari docs/BRD.md yang sudah disetujui, lalu rapikan lewat techwriter.",
                            roles.FSD, ws, budget("DOC", 2.0), roles.SUBAGENTS)

    # 4. SA -> SPEC teknis [gate]
    if i <= 3:
        mark("sa")
        await doc_with_gate("SA", "SPEC", "Jabarkan docs/FSD.md yang sudah disetujui menjadi docs/SPEC.md.",
                            roles.SA, ws, budget("SA", 1.5))

    # 5. Desainer -> DESIGN.md + token gaya [gate]
    if i <= 4:
        mark("design")
        await doc_with_gate("DESIGN", "DESIGN",
                            "Rancang tampilan aplikasi dari docs/FSD.md dan docs/SPEC.md "
                            "yang sudah disetujui, lalu tulis fondasi gaya di app/frontend/src/styles/.",
                            roles.DESIGNER, ws, budget("DOC", 4.0))

    # 6. Development
    if i <= 5:
        mark("dev")
        await run_stage_retry("LEAD", "Pecah docs/SPEC.md menjadi tiket dan delegasikan ke programmer "
                        "sampai aplikasi bisa dijalankan.",
                        roles.LEAD, ws, budget("DEV", 8.0), agents=roles.SUBAGENTS)

    # 7. QA + Pentest, loop perbaikan
    if i <= 6:
        mark("qa")
        if bug:
            # Perbaiki dulu apa yang dilaporkan, baru QA menguji. Kalau QA jalan
            # lebih dulu, laporan Bos cuma jadi tebakan yang harus ia temukan ulang.
            await run_stage_retry("LEAD-FIX", "Baca docs/BUG_REPORT.md (laporan bug dari pemilik "
                            "produk, bagian paling bawah paling baru) dan docs/FIXES.md, "
                            "perbaiki lewat programmer, lalu pastikan test hijau.",
                            roles.LEAD, ws, budget("DEV", 8.0), agents=roles.SUBAGENTS)
        # Tidak ada batas ronde. Tiap kali QA/pentest masih menemukan sesuatu,
        # Bos yang memutuskan: perbaiki lagi, atau tutup dengan temuan terbuka.
        n = 0
        while True:
            await run_stage_retry("QA", "Jalankan pengujian QA dan pentest, lalu susun FIXES.md.",
                            roles.QA_STAGE, ws, budget("QA", 3.0), agents=roles.SUBAGENTS)
            v_qa, v_pt = verdict(docs / "QA_REPORT.md"), verdict(docs / "PENTEST_REPORT.md")
            print(f"\n>>> Iterasi {n}: QA={v_qa}  PENTEST={v_pt}")
            monitor.emit("verdict", iteration=n, qa=v_qa, pentest=v_pt)
            monitor.set_status(verdict={"iteration": n, "qa": v_qa, "pentest": v_pt})
            monitor.tg_send(f"🧪 Iterasi {n}: QA={v_qa}, Pentest={v_pt}")
            if v_qa == "PASS" and v_pt == "PASS":
                break

            fx = docs / "FIXES.md"
            ans = await gate(
                f"Iterasi {n}: QA={v_qa}, Pentest={v_pt}. Ada temuan di docs/FIXES.md. "
                "'y' = perbaiki lalu uji ulang, 't' = tutup proyek dengan temuan terbuka, "
                "'q' = berhenti sekarang, teks lain = catatan untuk programmer lalu perbaiki.",
                files=[fx, docs / "QA_REPORT.md", docs / "PENTEST_REPORT.md"])
            low = ans.strip().lower()
            if low in ("t", "tidak", "n", "no"):
                fx.write_text(fx.read_text(encoding="utf-8") +
                              "\n\nDitutup oleh user dengan temuan di atas masih terbuka.\n",
                              encoding="utf-8")
                print(">>> Ditutup dengan temuan terbuka. Cek docs/FIXES.md.")
                break
            if low != "y":
                fx.write_text(fx.read_text(encoding="utf-8") + f"\n\nCatatan user:\n{ans}",
                              encoding="utf-8")
            await run_stage_retry("LEAD-FIX", "Kerjakan semua item di docs/FIXES.md lewat programmer, "
                            "lalu pastikan test hijau.",
                            roles.LEAD, ws, budget("DEV", 8.0), agents=roles.SUBAGENTS)
            n += 1

    cr.unlink(missing_ok=True)
    state.write_text("done", encoding="utf-8")
    monitor.set_status(stage="done", current="done")
    monitor.emit("info", msg="pipeline selesai")
    v = monitor._status.get("verdict") or {}
    vs = f" QA={v.get('qa', '?')}, Pentest={v.get('pentest', '?')}." if v else ""
    warn = " ADA TEMUAN BELUM DIPERBAIKI - cek docs/FIXES.md." if v.get("qa") == "FAIL" or v.get("pentest") == "FAIL" else ""
    monitor.tg_send(f"🏁 Pipeline {project} selesai.{vs}{warn} "
                    f"Total ~${monitor._status['cost'].get('total', 0):.2f}")
    print("\n" + "#" * 70)
    print(f"Selesai. Hasil di: {ws}")
    print("Dokumen: docs/PRD, BRD, FSD, SPEC, TASKS, QA_REPORT, PENTEST_REPORT")
    print("Kode: app/  — perintah run ada di SPEC.md bagian 'Cara menjalankan'.")
    print("#" * 70)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("idea", nargs="?", help="Ide aplikasi dalam satu kalimat")
    p.add_argument("--project", help="Nama folder proyek di workspace/")
    p.add_argument("--resume", choices=STAGES, default="ba",
                   help="Mulai dari tahap tertentu (butuh --project)")
    p.add_argument("--change", help="Permintaan perubahan/fitur baru untuk proyek yang sudah ada")
    p.add_argument("--bug", help="Laporan bug dari pemilik produk untuk aplikasi yang sudah jadi. Dokumen tidak diubah: langsung diperbaiki lalu diuji ulang")
    a = p.parse_args()

    if not a.idea and not a.project:
        p.error("butuh ide aplikasi atau --project")
    if a.change and not a.project:
        p.error("--change butuh --project")
    if a.bug and not a.project:
        p.error("--bug butuh --project")
    if a.bug and a.change:
        p.error("--bug dan --change tidak bisa dipakai bersamaan")
    if a.bug:
        a.resume = "qa"          # laporan bug tidak menyentuh tahap dokumen
    project = a.project or slugify(a.idea)
    # Ide baru yang slug-nya menabrak proyek lama. slugify() memotong di 40 karakter,
    # jadi dua ide berbeda bisa menghasilkan nama folder yang sama. Kalau diteruskan,
    # dokumen proyek baru ditulis ke folder yang app/-nya masih berisi kode proyek
    # lama - campuran rusak, tanpa peringatan. Berhenti dan minta nama lain.
    if a.idea and not a.project:
        docs_lama = ROOT / "workspace" / project / "docs"
        if docs_lama.exists():
            ide_lama = (docs_lama / "IDEA.md")
            ide_lama = ide_lama.read_text(encoding="utf-8").strip() if ide_lama.exists() else "(tidak tercatat)"
            sys.exit(
                f"\nFolder 'workspace/{project}' sudah dipakai proyek lain.\n"
                f"  Ide lama : {ide_lama}\n"
                f"  Ide baru : {a.idea}\n\n"
                f"Nama folder dipotong 40 karakter, jadi ide yang berbeda bisa bertabrakan.\n"
                f"Pilih salah satu:\n"
                f"  Lanjutkan yang lama    : python office.py --project {project}\n"
                f"  Tambah fitur ke sana   : python office.py --project {project} --change \"...\"\n"
                f"  Proyek baru terpisah   : python office.py \"{a.idea}\" --project <nama-lain>\n"
            )

    idea_file = ROOT / "workspace" / project / "docs" / "IDEA.md"
    idea = a.idea or (idea_file.read_text(encoding="utf-8") if idea_file.exists() else "")

    if not shutil.which("claude"):
        sys.exit("Claude Code CLI tidak ditemukan di PATH. Install: npm i -g @anthropic-ai/claude-code")

    print(f"Mode autentikasi: {auth_mode()}")
    st = ROOT / "workspace" / project / "docs" / "STATE.txt"

    # Tanpa --resume dan tanpa ide baru: lanjut otomatis dari STATE.txt
    if a.project and not a.idea and not a.change and not a.bug and a.resume == "ba" and st.exists():
        saved = st.read_text().strip()
        if saved in STAGES:
            a.resume = saved
            print(f"Melanjutkan otomatis dari tahap '{saved}' (docs/STATE.txt)")
        elif saved == "done":
            sys.exit("Proyek ini sudah selesai. Pakai --bug untuk melaporkan bug, "
                     "--change untuk fitur baru, atau --resume <tahap>.")

    lock_acquire(project, "bug" if a.bug else "change" if a.change else a.resume)
    try:
        asyncio.run(pipeline(idea, project, a.resume, a.change, a.bug))
    except (StageFailed, ResultError, KeyboardInterrupt) as e:
        stage = st.read_text().strip() if st.exists() else "?"
        why = "dihentikan (Ctrl+C)" if isinstance(e, KeyboardInterrupt) else str(e)
        monitor.emit("error", label="pipeline", error=why)
        monitor.tg_send(f"⚠ Pipeline berhenti di tahap '{stage}': {why}\nLanjut: python office.py --project {project}")
        sys.exit(f"\nPipeline berhenti di tahap '{stage}': {why}\n"
                 f"Lanjutkan dengan:\n"
                 f"  python office.py --project {project}")
    finally:
        lock_release()


if __name__ == "__main__":
    main()
