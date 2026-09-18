"""AI Office × BMAD — pipeline hybrid.

Dokumen & dev memakai skill BMAD (headless), gate/Telegram/kuota/resume/dashboard memakai AI Office.

  bmad-prd (headless) ─► open_questions? → gate Telegram → bmad-prd update ─► gate approve
  bmad-architecture (headless) ─► sama
  bmad-spec (headless) ─► SPEC.md
  Lead kita ─► stories.yaml ─► gate approve
  bmad-build-auto per story (berurutan) ─► checkpoint sesuai stories.yaml
  QA + Pentest kita ─► FIXES ─► bmad-build-auto "perbaiki FIXES" ─► ulang

Pakai:
    python office_bmad.py "aplikasi pencatat cuti karyawan"
    python office_bmad.py --project <nama>              # lanjut otomatis
    python office_bmad.py --project <nama> --change "tambah export Excel"
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

import monitor
import roles
from office import (ROOT, StageFailed, Sessions, gate, run_stage_retry, slugify, verdict,
                    auth_mode, budget)
import office

BMAD_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "Agent", "Skill"]
STAGES = ["prd", "arch", "spec", "stories", "dev", "qa"]


def bmad_role(model="opus"):
    """Peran tanpa system prompt custom: pakai default Claude Code + skill BMAD dari .claude/."""
    return dict(model=model, system_prompt=None, tools=BMAD_TOOLS, settings=["project"])


# ---------------------------------------------------------------------------
# BMAD install & helper
# ---------------------------------------------------------------------------
def bmad_install(ws: Path):
    if (ws / "_bmad").exists():
        return
    print(">>> Memasang BMAD (bmm) ke folder proyek ...")
    cmd = ["npx", "-y", "bmad-method@latest", "install", "--yes", "--directory", str(ws),
           "--modules", "bmm", "--tools", "claude-code", "--user-name", os.getenv("USER_NAME", "Bos"),
           "--communication-language", "Indonesian", "--document-output-language", "Indonesian"]
    r = subprocess.run(cmd, input="\n" * 12, text=True, capture_output=True, cwd=ws)
    if not (ws / "_bmad").exists():
        print(r.stdout[-2000:], r.stderr[-1000:])
        sys.exit("Instalasi BMAD gagal. Coba manual: cd workspace/<proyek> && npx bmad-method install")
    if not shutil.which("uv"):
        print("!! `uv` tidak ditemukan. Skill BMAD butuh uv: pip install uv  (atau curl -LsSf https://astral.sh/uv/install.sh | sh)")


def last_json(text: str) -> dict:
    """Ambil objek JSON terakhir dari teks balasan (kontrak headless BMAD)."""
    for m in reversed(list(re.finditer(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.S))):
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and "status" in d:
                return d
        except json.JSONDecodeError:
            continue
    return {}


def find_latest(ws: Path, pattern: str) -> Path | None:
    hits = sorted(ws.glob(pattern), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def rel(ws: Path, p) -> str:
    return str(Path(p).resolve().relative_to(ws.resolve())) if p else ""


HEADLESS = ("Jalankan dalam MODE HEADLESS: jangan bertanya, jangan menyapa, putuskan sendiri dan catat "
            "di assumptions[] / open_questions[]. Bahasa dokumen: Indonesia. "
            "Akhiri balasan dengan JSON status sesuai kontrak headless skill tersebut.\n")


async def bmad_doc_stage(label: str, skill: str, create_prompt: str, ws: Path,
                         doc_glob: str, budget_usd: float) -> Path:
    """Buat dokumen BMAD headless, lempar open_questions ke gate, ulangi update sampai disetujui."""
    docs = ws / "docs"
    res = await run_stage_retry(label, f"Gunakan skill `{skill}`. {HEADLESS}{create_prompt}",
                                bmad_role("opus"), ws, budget_usd)
    while True:
        js = last_json(res)
        doc = Path(js.get("prd") or js.get("architecture") or js.get("spec") or "") if js else None
        if not doc or not doc.exists():
            doc = find_latest(ws, doc_glob)
        if not doc:
            raise StageFailed(f"{label}: dokumen tidak ditemukan ({doc_glob})")
        oq = js.get("open_questions") or []
        asm = js.get("assumptions") or []
        (docs / f"{label}_STATUS.json").write_text(json.dumps(js, ensure_ascii=False, indent=1))

        msg = f"{label} selesai: {rel(ws, doc)}"
        if asm:
            msg += "\n\nAsumsi yang diambil AI:\n" + "\n".join(f"- {a}" for a in asm[:10])
        if oq:
            msg += "\n\nPERTANYAAN TERBUKA (jawab satu per satu, atau 'y' untuk terima apa adanya):\n" + \
                   "\n".join(f"{i+1}. {q}" for i, q in enumerate(oq))
        ans = await gate(msg, label)
        if ans.lower() == "y":
            return doc
        # jawaban/feedback → update headless
        res = await run_stage_retry(
            f"{label}-UPD",
            f"Gunakan skill `{skill}`. {HEADLESS}intent: update. Dokumen: {rel(ws, doc)}. "
            f"Change signal dari pemilik produk (jawaban atas open_questions / koreksi):\n{ans}",
            bmad_role("opus"), ws, budget_usd)


def load_stories(spec_dir: Path) -> list[dict]:
    f = spec_dir / "stories.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else []
    return data or []


def story_status(spec_dir: Path, sid: str) -> str:
    for f in spec_dir.glob(f"stories/{sid}-*.md"):
        m = re.search(r"^status:\s*(\S+)", f.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "?"
    return "none"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def pipeline(idea: str, project: str, start: str, change: str | None):
    ws = ROOT / "workspace" / project
    docs = ws / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    office.SESSIONS = Sessions(docs)
    monitor.init(docs, project)
    monitor.set_status(bmad=True)
    state = docs / "STATE.txt"

    def mark(st):
        state.write_text(st, encoding="utf-8")
        monitor.set_status(stage=st)

    bmad_install(ws)
    cr = docs / "CHANGE_REQUEST.md"
    if change:
        cr.write_text(change, encoding="utf-8")
        start = "prd"
        monitor.emit("info", msg=f"change request: {change}")
    else:
        cr.unlink(missing_ok=True)
        (docs / "IDEA.md").write_text(idea, encoding="utf-8")
    i = STAGES.index(start)
    ctx = json.loads((docs / "BMAD_PATHS.json").read_text()) if (docs / "BMAD_PATHS.json").exists() else {}

    def save_ctx():
        (docs / "BMAD_PATHS.json").write_text(json.dumps(ctx, indent=1))

    # 1. PRD -------------------------------------------------------------------
    if i <= 0:
        mark("prd")
        if change and ctx.get("prd"):
            p = (f"intent: update. Dokumen: {ctx['prd']}. Change signal: {change}. "
                 f"Tandai bagian yang berubah/ditambah dengan [v1.1].")
        else:
            p = f"intent: create. Brief/ide produk:\n{idea}\nStakes: internal. Form factor: web."
        doc = await bmad_doc_stage("PRD", "bmad-prd", p, ws,
                                   "_bmad-output/planning-artifacts/prds/*/prd.md", budget("DOC", 3.0))
        ctx["prd"] = rel(ws, doc); save_ctx()

    # 2. Architecture ------------------------------------------------------------
    if i <= 1:
        mark("arch")
        p = (f"intent: {'update' if change and ctx.get('arch') else 'create'}. PRD: {ctx['prd']}. "
             + (f"Dokumen: {ctx['arch']}. Change signal: {change}. " if change and ctx.get('arch') else "")
             + "Pilih stack paling sederhana yang cukup (default FastAPI + React/Vite + SQLite).")
        doc = await bmad_doc_stage("ARCH", "bmad-architecture", p, ws,
                                   "_bmad-output/planning-artifacts/architecture/*/*.md", budget("DOC", 3.0))
        ctx["arch"] = rel(ws, doc); save_ctx()

    # 3. SPEC ---------------------------------------------------------------------
    if i <= 2:
        mark("spec")
        slug = slugify(project)
        p = (f"{HEADLESS}Input: {ctx['prd']} dan {ctx['arch']}"
             + (f" dan docs/CHANGE_REQUEST.md (update spec yang ada, tandai [v1.1])" if change else "")
             + f". slug: {slug}. Distill menjadi SPEC.md.")
        res = await run_stage_retry("SPEC", f"Gunakan skill `bmad-spec`. {p}", bmad_role("opus"), ws,
                                    budget("SA", 2.0))
        js = last_json(res)
        files = js.get("files") or []
        spec = next((Path(ws / f) for f in files if f.endswith("SPEC.md")), None) or \
            find_latest(ws, "_bmad-output/specs/*/SPEC.md")
        if not spec:
            raise StageFailed("SPEC: SPEC.md tidak ditemukan")
        ctx["spec_dir"] = rel(ws, spec.parent); save_ctx()

    # 4. Stories (Lead kita) + gate --------------------------------------------------
    spec_dir = ws / ctx["spec_dir"]
    if i <= 3:
        mark("stories")
        lead = dict(model="sonnet", system_prompt=roles.load("stories"), tools=roles.CODE_TOOLS,
                    settings=["project"])
        while True:
            await run_stage_retry("STORIES", f"Folder spec: {ctx['spec_dir']}. Buat stories.yaml.",
                                  lead, ws, budget("SA", 1.5))
            stories = load_stories(spec_dir)
            listing = "\n".join(f"{s['id']}. {s['title']}" + (" [checkpoint]" if s.get("spec_checkpoint") else "")
                                for s in stories)
            ans = await gate(f"stories.yaml ({len(stories)} story) di {ctx['spec_dir']}:\n{listing}\n\n"
                             "Setuju? (atau tulis perubahan yang diinginkan)", "STORIES")
            if ans.lower() == "y":
                break
            (docs / "STORIES_FEEDBACK.md").write_text(ans, encoding="utf-8")

    # 5. Dev: bmad-build-auto per story ------------------------------------------------
    if i <= 4:
        mark("dev")
        for s in load_stories(spec_dir):
            sid = str(s["id"])
            st = story_status(spec_dir, sid)
            if st == "done":
                continue
            if s.get("spec_checkpoint") and st == "none":
                # biarkan build-auto membuat spec story dulu (status ready-for-dev), lalu review
                pass
            monitor.set_status(current=f"DEV story {sid}")
            res = await run_stage_retry(
                f"DEV-{sid}",
                f"Gunakan skill `bmad-build-auto`. spec folder: {ctx['spec_dir']}. story id: {sid}. "
                + (s.get("invoke_dev_with") or ""),
                bmad_role("sonnet"), ws, budget("DEV", 6.0))
            st = story_status(spec_dir, sid)
            monitor.emit("verdict", iteration=0, qa=f"story {sid}", pentest=st)
            if st == "blocked":
                ans = await gate(f"Story {sid} ({s['title']}) BLOCKED. Cek {ctx['spec_dir']}/stories/. "
                                 "'y' = lewati dan lanjut, atau tulis instruksi untuk dicoba lagi.", f"DEV-{sid}")
                if ans.lower() != "y":
                    await run_stage_retry(f"DEV-{sid}-R",
                                          f"Gunakan skill `bmad-build-auto`. spec folder: {ctx['spec_dir']}. "
                                          f"story id: {sid}. Catatan pemilik produk: {ans}",
                                          bmad_role("sonnet"), ws, budget("DEV", 6.0))
            elif s.get("done_checkpoint"):
                await gate(f"Story {sid} selesai (done_checkpoint). Lanjut ke story berikutnya?", f"DEV-{sid}")

    # 6. QA + Pentest kita, perbaikan via build-auto ----------------------------------------
    if i <= 5:
        mark("qa")
        # dokumen rujukan untuk prompt QA/pentest kita
        shutil.copy(spec_dir / "SPEC.md", docs / "SPEC.md")
        shutil.copy(ws / ctx["prd"], docs / "FSD.md")
        note = ("CATATAN LOKASI: kode aplikasi ada di ROOT folder proyek (bukan app/). "
                "Perintah run/test ada di docs/SPEC.md atau README. ")
        max_loop = int(os.getenv("MAX_FIX_LOOP", 2))
        qa_role = dict(roles.QA_STAGE); qa_role["system_prompt"] = note + qa_role["system_prompt"]
        for n in range(max_loop + 1):
            await run_stage_retry("QA", note + "Jalankan pengujian QA dan pentest, lalu susun docs/FIXES.md.",
                                  qa_role, ws, budget("QA", 3.0), agents=roles.SUBAGENTS)
            v_qa, v_pt = verdict(docs / "QA_REPORT.md"), verdict(docs / "PENTEST_REPORT.md")
            monitor.emit("verdict", iteration=n, qa=v_qa, pentest=v_pt)
            monitor.set_status(verdict={"iteration": n, "qa": v_qa, "pentest": v_pt})
            monitor.tg_send(f"🧪 Iterasi {n}: QA={v_qa}, Pentest={v_pt}")
            if v_qa == "PASS" and v_pt == "PASS":
                break
            if n == max_loop:
                print(">>> Batas iterasi perbaikan tercapai. Cek docs/FIXES.md manual.")
                break
            ans = await gate("Ada temuan (docs/FIXES.md). Jalankan perbaikan otomatis lewat bmad-build-auto?", "FIX")
            extra = "" if ans.lower() == "y" else f"\nCatatan pemilik produk: {ans}"
            await run_stage_retry("FIX", f"Gunakan skill `bmad-build-auto`. Intent: perbaiki semua temuan di "
                                  f"docs/FIXES.md (urutkan dari severity tertinggi).{extra}",
                                  bmad_role("sonnet"), ws, budget("DEV", 6.0))

    cr.unlink(missing_ok=True)
    state.write_text("done", encoding="utf-8")
    monitor.set_status(current="done")
    monitor.tg_send(f"🏁 Pipeline BMAD {project} selesai. Total ~${monitor._status['cost'].get('total', 0):.2f}")
    print(f"\nSelesai. Proyek: {ws}\nDokumen BMAD: _bmad-output/  |  Laporan QA/Pentest: docs/")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("idea", nargs="?")
    p.add_argument("--project")
    p.add_argument("--resume", choices=STAGES, default="prd")
    p.add_argument("--change")
    a = p.parse_args()
    if not a.idea and not a.project:
        p.error("butuh ide aplikasi atau --project")
    project = a.project or slugify(a.idea)
    docs = ROOT / "workspace" / project / "docs"
    idea = a.idea or ((docs / "IDEA.md").read_text() if (docs / "IDEA.md").exists() else "")
    st = docs / "STATE.txt"
    if a.project and not a.idea and not a.change and a.resume == "prd" and st.exists():
        saved = st.read_text().strip()
        if saved in STAGES:
            a.resume = saved; print(f"Melanjutkan otomatis dari tahap '{saved}'")
        elif saved == "done":
            sys.exit("Proyek sudah selesai. Pakai --change untuk fitur baru atau --resume <tahap>.")
    for tool in ("claude", "npx"):
        if not shutil.which(tool):
            sys.exit(f"`{tool}` tidak ditemukan di PATH.")
    print(f"Mode autentikasi: {auth_mode()}")
    try:
        asyncio.run(pipeline(idea, project, a.resume, a.change))
    except (StageFailed, KeyboardInterrupt) as e:
        stage = st.read_text().strip() if st.exists() else "?"
        why = "dihentikan (Ctrl+C)" if isinstance(e, KeyboardInterrupt) else str(e)
        monitor.tg_send(f"⚠ Pipeline BMAD berhenti di '{stage}': {why}")
        sys.exit(f"\nBerhenti di tahap '{stage}': {why}\nLanjutkan: python office_bmad.py --project {project}")


if __name__ == "__main__":
    main()
