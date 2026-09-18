# AI Office — PoC kantor software dengan karyawan AI

Pipeline:

```
BA (PRD)
 → SA + Technical Writer: BRD  → [gate 1: Bos review BRD]
 → SA + Technical Writer: FSD  → [gate 2: Bos review FSD]
 → SA: SPEC teknis            → [gate 3: Bos review SPEC]
 → Designer: DESIGN + token   → [gate 4: Bos review tampilan]
 → Lead + Programmer (paralel)
 → QA + Pentest (paralel) → FIXES → [gate: perbaiki?] → Lead-Fix → ulang
```

Di setiap gate Bos bisa ketik `y` (setuju), tulis feedback (dokumen direvisi lalu ditanya lagi), atau `q` (berhenti).

Dibangun di atas Claude Agent SDK (Python). Tiap tahap adalah satu `query()` terpisah,
jadi konteks tiap peran bersih dan biaya per tahap bisa dibatasi.

## Struktur

```
ai-office/
├── office.py        # orchestrator / pipeline
├── monitor.py       # event log, status, Telegram
├── dashboard.py     # dashboard web lokal + panel kendali
├── control.py       # menjalankan office.py dari dashboard/Telegram, kunci satu-pipeline
├── roles.py         # definisi peran (model, tool, subagent)
├── prompts/*.md     # system prompt per peran — edit di sini untuk ubah perilaku
├── prompts/_standar.md  # standar kantor, otomatis menempel ke semua peran
├── .env.example
└── workspace/<project>/
    ├── docs/        # IDEA, PRD, SPEC, TASKS, QA_REPORT, PENTEST_REPORT, FIXES
    └── app/         # kode aplikasi hasil kerja programmer
```

## Setup

```bash
# 1. Claude Code CLI (kalau belum)
npm i -g @anthropic-ai/claude-code

# 2. Python env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Konfigurasi
cp .env.example .env
# Pakai langganan Claude (Pro/Max): biarkan ANTHROPIC_API_KEY kosong, pastikan sudah `claude` -> /login
# Pakai API key: isi ANTHROPIC_API_KEY

# 4. (opsional, untuk pentest) tool static analysis
pip install semgrep bandit
```

## Jalankan

```bash
python office.py "aplikasi pencatat cuti karyawan dengan approval atasan"
```

Satu instalasi menampung banyak proyek — tiap ide baru membuat `workspace/<nama>/`
sendiri dengan `docs/` dan `app/` terpisah. Nama foldernya diturunkan dari ide dan
**dipotong 40 karakter**, jadi dua ide yang awalannya mirip bisa menghasilkan nama
yang sama. Kalau itu terjadi pipeline berhenti dan menawarkan pilihan, bukan menimpa
folder lama — pakai `--project <nama>` untuk menentukan namanya sendiri:

```bash
python office.py "aplikasi kasir warung" --project kasir-warung
```

Pipeline berhenti di dua titik untuk minta persetujuan Bos:
1. Setelah SPEC.md jadi — ketik `y` untuk lanjut, atau tulis feedback → SA revisi.
2. Setelah QA/Pentest menemukan masalah — `y` untuk perbaikan otomatis.

Kalau proses putus (kuota habis, Ctrl+C, PC mati), lanjutkan — tahapnya dibaca otomatis dari `docs/STATE.txt`:

```bash
python office.py --project aplikasi-pencatat-cuti
# atau paksa tahap tertentu (ba|brd|fsd|sa|dev|qa):
python office.py --project aplikasi-pencatat-cuti --resume dev
```

Session ID tiap tahap disimpan di `docs/SESSIONS.json`. Saat resume, sesi Claude Code yang terputus
dilanjutkan (konteks Lead/QA ikut pulih), dengan instruksi untuk mengecek keadaan disk dulu.
Kalau sesi lama tidak bisa dibuka, tahap dimulai dengan sesi baru dan mengandalkan `docs/TASKS.md`.
Subagent (programmer) yang sedang berjalan saat putus tidak ikut pulih — tiketnya dikerjakan ulang.

Supaya proses tidak ikut mati saat terminal ditutup atau laptop sleep, jalankan di `tmux`/`screen`,
dan matikan sleep saat pipeline berjalan.

Tambah fitur / ubah proses bisnis pada proyek yang sudah jadi (change request):

```bash
python office.py --project aplikasi-pencatat-cuti --change "tambah approval berjenjang: atasan lalu HRD"
```

Semua tahap berjalan ulang secara *incremental*: PRD/BRD/FSD/SPEC hanya ditambah bagian bertanda `[v1.1]`,
programmer hanya mengerjakan delta, QA menguji fitur baru plus regresi fitur lama.

## Mode hybrid dengan BMAD (`office_bmad.py`)

Versi kedua pipeline yang memakai skill **BMAD-METHOD v6** untuk dokumen dan dev, sementara gate,
Telegram, penanganan kuota, resume, dan dashboard tetap dari AI Office.

```
bmad-prd (headless) ──► pertanyaan terbuka → Telegram → update ──► approve
bmad-architecture (headless) ──► sama
bmad-spec (headless) ──► SPEC.md
Lead kita ──► stories.yaml ──► approve
bmad-build-auto per story (plan → implement → review, berurutan)
QA + Pentest kita ──► FIXES ──► bmad-build-auto ──► ulang
```

```bash
pip install uv                      # skill BMAD butuh uv
python office_bmad.py "aplikasi pencatat cuti karyawan"
python office_bmad.py --project <nama>                     # lanjut
python office_bmad.py --project <nama> --change "tambah export Excel"
```

BMAD dipasang otomatis per folder proyek (`workspace/<proyek>/_bmad`, `.claude/skills`) saat run pertama.
Dokumen BMAD ada di `_bmad-output/`, laporan QA/Pentest di `docs/`. Kode ditulis BMAD di root folder proyek.
`stories.yaml` bisa diedit sebelum approve: `spec_checkpoint: true` = review spec story sebelum dikoding,
`done_checkpoint: true` = berhenti setelah story itu selesai.

Perbedaan dengan `office.py`: tidak ada BRD/FSD (diganti PRD + Architecture BMAD), story dikerjakan
berurutan bukan paralel, dan kualitas dokumen mengikuti template BMAD.

## Punya BRD, FSD, atau contoh desain sendiri?

Unggah lewat panel dashboard (**Bahan acuan**) atau taruh manual di
`workspace/<proyek>/docs/referensi/`. Format yang diterima: `.md`, `.txt`, `.json`,
`.csv`, `.pdf`, dan gambar (`.png`, `.jpg`, `.webp`, `.gif`), maksimal 10 MB per berkas.

**Pipeline tetap berjalan penuh** — BA, BRD, FSD, SPEC, Designer, dev, QA, berikut
semua gate-nya. Bahan itu tidak melompati tahap mana pun; ia dibaca setiap peran
sebagai acuan kuat: keputusan yang sudah Anda ambil diikuti, bagian yang belum
tercakup tetap dikerjakan, dan kalau ada yang bertentangan atau keliru, peran itu
mengerjakannya sesuai bahan Anda lalu menuliskan keberatannya di `KESENJANGAN`
supaya muncul di gate — bukan menyimpang diam-diam. Aturannya ada di
`prompts/_standar.md`, jadi berlaku untuk sebelas peran sekaligus.

Alur yang disarankan: buat proyek lewat panel **Proyek baru**, unggah bahannya,
baru jalankan. Mengunggah sebelum pipeline dimulai diperbolehkan.

Berbeda dengan `--resume <tahap>`, yang benar-benar melompati tahap sebelumnya dan
memakai dokumen di `docs/` apa adanya. Pakai itu hanya kalau Anda memang ingin
memotong prosesnya; kalau dokumen masukan tahap tujuan tidak ada, pipeline berhenti
dengan pesan jelas alih-alih mengarang.

## Lapor bug setelah aplikasi jadi

Kalau kamu memakai aplikasinya dan menemukan bug, jangan pakai `--change` — itu
menjalankan ulang seluruh rantai dokumen dan mencatat bugmu sebagai perubahan
kebutuhan, padahal kebutuhannya tidak berubah. Pakai `--bug`:

```bash
python office.py --project <nama> --bug "Angka 6 tidak menggerakkan bidak, harusnya maju 6 lalu lempar lagi (AC-08-1)"
```

Laporan disimpan di `docs/BUG_REPORT.md` dan disisipkan sebagai item wajib di
`docs/FIXES.md`. PRD/BRD/FSD/SPEC tidak disentuh. Lead langsung memperbaiki,
lalu QA menguji — dengan kewajiban membuat skenario yang mereproduksi laporanmu,
sehingga PASS berarti "bugmu terbukti hilang", bukan "aku tidak menemukannya".

## Menjalankan tanpa terminal

`dashboard.py` bukan cuma pemantau, tapi juga pusat kendali:

- **Dari browser** — panel di atas halaman: tombol Lanjutkan / Resume / Hentikan,
  dan kotak teks untuk lapor bug atau minta fitur. Tombol nonaktif otomatis saat
  ada pipeline berjalan.
- **Dari Telegram** — `/status`, `/bug <teks>`, `/ubah <teks>`, `/lanjut`,
  `/resume <tahap>`, `/stop`, `/proyek <nama>`.

Satu pipeline saja yang boleh jalan: `office.py` menulis `.pipeline.lock` berisi
PID-nya dan menghapusnya saat selesai. Kalau proses mati mendadak dan kuncinya
tertinggal, ada tombol "Hapus kunci basi" di panel.

**Penting soal Telegram:** perintah hanya didengarkan saat TIDAK ada pipeline
berjalan. Antrean `getUpdates` milik bot cuma satu dan offset-nya bersama — kalau
dashboard ikut membaca saat pipeline menunggu di gate, jawaban gate bisa tertelan
dashboard dan hilang. Jadi saat pipeline hidup, chat itu sepenuhnya milik gate.

## Monitoring realtime

**Dashboard web** — jalankan di terminal kedua:

```bash
python dashboard.py     # buka http://localhost:8765 (bisa dari HP di jaringan yang sama)
```

Menampilkan: tahap aktif, gate yang sedang menunggu, log aktivitas agent live (pesan, subagent
yang dipanggil, tool yang dijalankan), tiket dari `TASKS.md` dengan statusnya, kuota langganan
(persen terpakai & jam reset — hanya dikirim server saat mendekati batas), estimasi biaya per tahap,
dan isi laporan QA/Pentest/FIXES + semua dokumen. Refresh otomatis tiap 2 detik.

### Keamanan dashboard

Dashboard bukan pemantau pasif: ia bisa menjalankan pipeline, dan agent di dalamnya
punya akses `Bash`. Teks yang dikirim lewat "Lapor bug" berakhir di dokumen yang
dibaca programmer. Artinya **akses ke port dashboard setara akses shell ke mesin itu.**

Karena itu bawaannya `DASHBOARD_HOST=127.0.0.1` — hanya dari mesin sendiri. Untuk
mengaksesnya dari jauh, cara yang benar adalah terowongan SSH, tanpa membuka port:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

Kalau memang harus dibuka ke jaringan, `DASHBOARD_PASS` wajib diisi — tanpa itu
dashboard **menolak berjalan**. Basic auth lewat HTTP polos mengirim sandi tanpa
enkripsi, jadi untuk pemakaian sungguhan taruh di belakang reverse proxy ber-TLS.

`DENY_BASH` di `roles.py` memblokir beberapa perintah berbahaya, tapi itu daftar
pola harfiah — anggap sebagai polisi tidur, bukan batas keamanan. Untuk isolasi
sungguhan, jalankan seluruh folder ini di dalam container.

**Telegram** — isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di `.env`. Bos dapat pesan saat
tahap mulai/selesai, gate menunggu, kuota habis, hasil QA/Pentest, dan error. Gate bisa dijawab
langsung dari Telegram (`y`, `q`, atau teks feedback) — tidak harus di depan terminal.

Semua event tersimpan di `workspace/<project>/docs/events.jsonl`, snapshot di `status.json`.
Kalau nanti mau masuk PostgreSQL/Grafana, cukup baca dua file itu.

## Pakai host AI pihak ketiga

Di `.env` set `AI_PROVIDER=custom`, lalu isi `CUSTOM_BASE_URL`, `CUSTOM_API_KEY`, dan
`CUSTOM_MODEL`. Alias `opus`/`sonnet`/`haiku` di setiap peran otomatis dipetakan ke
model itu; bisa dibedakan per tingkat lewat `CUSTOM_MODEL_OPUS` dan kawan-kawan.
Kembali ke langganan Claude: `AI_PROVIDER=claude`.

**Syarat mutlak: host harus melayani skema Anthropic (`/v1/messages`).** Agent di sini
berjalan di atas Claude Code, yang tidak bisa berbicara skema OpenAI. Banyak gateway
(LiteLLM, one-api, dan sejenisnya) menyediakan keduanya — cek dulu:

```bash
curl <host>/v1/messages -H "x-api-key: <kunci>" -H "anthropic-version: 2023-06-01"   -H "Content-Type: application/json"   -d '{"model":"<model>","max_tokens":20,"messages":[{"role":"user","content":"halo"}]}'
```

Kalau modelnya bukan Claude, tiga hal berubah dan perlu disadari:

- **Plafon biaya tidak bisa diandalkan.** Claude Code tidak mengenal nama modelnya dan
  gateway sering melaporkan token 0, sehingga angka biaya di dashboard tidak akurat
  dan `BUDGET_*` bisa tidak pernah terpicu. Awasi pemakaian dari sisi penyedia.
- **Eskalasi model tidak berarti apa-apa** kalau ketiga alias dipetakan ke model yang
  sama — "naik ke opus" tetap memanggil model yang sama.
- **Seluruh dokumen dan kode proyek dikirim ke host tersebut.**

## Pakai OpenRouter sebagai cadangan

Di `.env` set `USE_OPENROUTER=1` dan isi `OPENROUTER_API_KEY`. Hanya model Anthropic
yang berjalan lewat jalur ini (Claude Code butuh semantik API Anthropic).

## Pembagian model (atur di `.env`, bukan di kode)

| Peran | Bawaan | Alasan |
|---|---|---|
| BA, SA (BRD/FSD/SPEC), Designer | opus | butuh penalaran dan penilaian, keluaran kecil |
| Technical Writer | sonnet | merapikan dokumen, tidak ubah substansi |
| Lead, Programmer, QA, Pentest | sonnet | volume token besar, tugas terstruktur |

Semuanya bisa ditimpa lewat `.env` tanpa menyentuh kode — `MODEL_<PERAN>=opus`.
Nama peran yang tersedia: `BA`, `BRD`, `FSD`, `SA`, `DESIGNER`, `LEAD`, `QA_STAGE`,
`TECHWRITER`, `PROGRAMMER`, `QA`, `PENTEST`. Nilai: `haiku`, `sonnet`, `opus`.

**Naik model saat gagal.** Kalau sebuah tahap gagal, gate menawarkan mengulangnya
dengan model satu tingkat lebih tinggi (`haiku → sonnet → opus`), karena gagal
berulang biasanya berarti tahapnya terlalu berat untuk model itu — bukan sekadar
sial. Kenaikan berlaku untuk tahap itu saja dan tidak bocor ke tahap lain.
Kalau sudah di `opus`, gate mengatakannya terus terang dan mengulang apa adanya.

## Batasan yang disengaja

- `permission_mode="acceptEdits"` + Bash diizinkan → programmer bisa jalankan test.
  Perintah berbahaya (`rm -rf`, `sudo`, `git push`) ditolak lewat `DENY_BASH`.
  Untuk keamanan lebih, jalankan seluruh folder ini di dalam container Docker.
- `max_budget_usd` per tahap membatasi biaya kalau agent berputar-putar.
- Gate manual ada di orchestrator, bukan di subagent (subagent tidak bisa minta approval).
- Loop perbaikan tidak dibatasi angka: tiap ronde Bos ditanya `y` (perbaiki lagi),
  `t` (tutup dengan temuan terbuka), atau `q` (berhenti). `MAX_FIX_LOOP` kini hanya
  dipakai `office_bmad.py`.

## Yang belum ada (langkah berikutnya kalau PoC berhasil)

- Sandbox Docker per tiket programmer.
- Dashboard status (bisa pakai pola PostgreSQL + laporan seperti SOC report).
- Notifikasi gate lewat Telegram/WA supaya tidak harus menunggu di terminal.
- Peran deployer (build image, deploy ke staging).
