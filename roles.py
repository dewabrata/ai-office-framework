"""Definisi peran "karyawan AI".

Peran yang dipanggil langsung oleh orchestrator (BA, SA, LEAD, QA-stage) dipakai
sebagai system_prompt + model pada query(). Peran yang dipanggil oleh peran lain
(programmer, qa, pentest) didefinisikan sebagai AgentDefinition (subagent).
"""
import os
from pathlib import Path

from claude_agent_sdk import AgentDefinition
from dotenv import load_dotenv

# roles.py diimpor SEBELUM office.py memanggil load_dotenv(), jadi .env dimuat
# di sini juga. Tanpa ini setelan MODEL_* di .env akan diabaikan diam-diam.
load_dotenv()

PROMPTS = Path(__file__).parent / "prompts"

# Urutan kekuatan model, dipakai untuk menaikkan model saat sebuah tahap gagal.
ESKALASI = ["haiku", "sonnet", "opus"]


def model_for(peran: str, bawaan: str) -> str:
    """Model untuk satu peran, bisa ditimpa lewat .env (mis. MODEL_LEAD=opus)."""
    return os.getenv(f"MODEL_{peran}", bawaan).strip() or bawaan


def naik_model(model: str) -> str | None:
    """Model satu tingkat di atas, atau None kalau sudah yang tertinggi."""
    if model not in ESKALASI:
        return None
    i = ESKALASI.index(model)
    return ESKALASI[i + 1] if i + 1 < len(ESKALASI) else None


def load(name: str, standar: bool = True) -> str:
    """Muat prompt peran. `standar=True` menempelkan standar kantor (wajib
    menantang masukan + melapor KESENJANGAN) supaya aturannya cukup ditulis
    sekali di prompts/_standar.md, bukan disalin ke tiap peran.

    techwriter dikecualikan: tugasnya merapikan naskah, bukan menilai produk.
    """
    teks = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    if standar:
        teks += "\n" + (PROMPTS / "_standar.md").read_text(encoding="utf-8")
    return teks


# Tool yang aman untuk peran yang cuma menulis dokumen
DOC_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
# Tool untuk peran yang mengeksekusi kode
CODE_TOOLS = DOC_TOOLS + ["Bash"]

# Perintah shell yang selalu ditolak, di semua peran
DENY_BASH = [
    "Bash(rm -rf *)",
    "Bash(sudo *)",
    "Bash(curl * | sh)",
    "Bash(curl * | bash)",
    "Bash(git push *)",
]

# ---- Peran level orchestrator ---------------------------------------------

BA = dict(model=model_for("BA", "opus"), system_prompt=load("ba"), tools=DOC_TOOLS + ["WebSearch"])
BRD = dict(model=model_for("BRD", "opus"), system_prompt=load("brd"), tools=DOC_TOOLS + ["Agent"])
FSD = dict(model=model_for("FSD", "opus"), system_prompt=load("fsd"), tools=DOC_TOOLS + ["Agent"])
SA = dict(model=model_for("SA", "opus"), system_prompt=load("sa"), tools=DOC_TOOLS)
# Desainer perlu menulis berkas gaya di app/, jadi butuh tool tulis; opus karena
# ini pekerjaan penilaian rasa, bukan penerjemahan mekanis.
DESIGNER = dict(model=model_for("DESIGNER", "opus"), system_prompt=load("designer"), tools=DOC_TOOLS)
LEAD = dict(model=model_for("LEAD", "sonnet"), system_prompt=load("lead"), tools=CODE_TOOLS + ["Agent"])
QA_STAGE = dict(
    model=model_for("QA_STAGE", "sonnet"),
    system_prompt=(
        "Kamu koordinator QA. Panggil subagent `qa` dan `pentest` SECARA BERSAMAAN "
        "dalam satu pesan (paralel). Tunggu keduanya selesai. Lalu baca "
        "docs/QA_REPORT.md dan docs/PENTEST_REPORT.md, dan tulis docs/FIXES.md: "
        "daftar perbaikan yang wajib dikerjakan programmer, diurutkan dari severity "
        "tertinggi, tiap item menyebut file/endpoint dan langkah perbaikan. "
        "Kalau kedua laporan PASS, tulis FIXES.md berisi satu baris: TIDAK ADA."
        + "\n" + (PROMPTS / "_standar.md").read_text(encoding="utf-8")
    ),
    tools=CODE_TOOLS + ["Agent"],
)

# ---- Subagent -------------------------------------------------------------

SUBAGENTS = {
    "techwriter": AgentDefinition(
        description="Merapikan dokumen BRD/FSD: struktur, istilah, penomoran, tanpa mengubah substansi.",
        prompt=load("techwriter", standar=False),
        tools=DOC_TOOLS,
        model=model_for("TECHWRITER", "sonnet"),
        maxTurns=25,
    ),
    "programmer": AgentDefinition(
        description="Mengerjakan satu tiket coding sampai selesai termasuk unit test.",
        prompt=load("programmer"),
        tools=CODE_TOOLS,
        model=model_for("PROGRAMMER", "sonnet"),
        maxTurns=60,
    ),
    "qa": AgentDefinition(
        description="Menguji fungsionalitas aplikasi dan menulis docs/QA_REPORT.md.",
        prompt=load("qa"),
        tools=CODE_TOOLS,
        model=model_for("QA", "sonnet"),
        maxTurns=50,
    ),
    "pentest": AgentDefinition(
        description="Uji keamanan aplikasi dan menulis docs/PENTEST_REPORT.md.",
        prompt=load("pentest"),
        tools=CODE_TOOLS,
        model=model_for("PENTEST", "sonnet"),
        maxTurns=50,
    ),
}
