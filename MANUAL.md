# Manual Instalasi & Deployment — AI Office (PoC)

Panduan ini untuk menjalankan AI Office di laptop/PC sendiri dengan langganan Claude (Pro/Max).
Perkiraan waktu setup: 20–30 menit.

---

## 1. Prasyarat

| Komponen | Versi minimum | Cek |
|---|---|---|
| Sistem operasi | Linux, macOS, atau Windows 10/11 (native atau WSL2) | — |
| Python | 3.10 atau lebih baru | `python3 --version` |
| Node.js | 18 atau lebih baru | `node --version` |
| Git | terbaru | `git --version` |
| Akun Claude | Pro atau Max (Max disarankan, PoC boros kuota) | login di claude.ai |

### Pilihan untuk Windows

| | Native Windows (PowerShell) | WSL2 (Ubuntu) |
|---|---|---|
| Setup | Lebih sederhana | Perlu `wsl --install` + restart |
| Tool Bash | Butuh Git for Windows (wajib untuk AI Office) | Bawaan |
| Isolasi | Programmer jalan langsung di Windows | Terpisah dari Windows, bisa pakai Docker |
| tmux | Tidak ada, pakai tab Windows Terminal | Ada |
| semgrep (pentest) | Dukungan terbatas | Penuh |
| Status uji | Belum diuji | Paling dekat dengan lingkungan uji |

Rekomendasi: **native kalau mau cepat coba**, **WSL2 kalau mau lebih aman** atau nanti pakai Docker.
Perintah di manual ini ditulis untuk Linux/WSL/macOS; padanan Windows native diberi tanda **(Win)**.

Pasang WSL2 (kalau memilih WSL): buka PowerShell sebagai Administrator, jalankan `wsl --install`, restart, lalu semua perintah berikutnya dijalankan di dalam Ubuntu.

**(Win)** Prasyarat native: pasang [Git for Windows](https://git-scm.com/downloads/win) (pilih opsi "Git from the command line and also from 3rd-party software"), Python 3.10+ dari python.org (centang "Add to PATH"), dan Node.js 18+.

Kalau Node.js belum ada (Ubuntu/WSL):

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

Kalau Python di bawah 3.10 (Ubuntu 20.04 ke bawah):

```bash
sudo apt-get install -y python3.11 python3.11-venv
# lalu ganti semua "python3" di manual ini dengan "python3.11"
```

---

## 2. Pasang Claude Code dan login

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

**(Win)** Di PowerShell: `irm https://claude.ai/install.ps1 | iex`, lalu buka terminal baru dan `claude --version`.
Kalau Claude Code mengeluh tidak menemukan Git Bash, set di `%USERPROFILE%\.claude\settings.json`:

```json
{ "env": { "CLAUDE_CODE_GIT_BASH_PATH": "C:\\Program Files\\Git\\bin\\bash.exe" } }
```

Login dengan akun langganan:

```bash
claude
```

Di dalam Claude Code ketik `/login`, pilih **Claude account with subscription**, ikuti tautan yang muncul di browser, lalu kembali ke terminal. Setelah berhasil, ketik `/exit`.

Verifikasi login tersimpan:

```bash
claude -p "jawab satu kata: siap"
```

Harus keluar jawaban tanpa diminta API key. Kalau muncul error soal API key atau billing, ulangi `/login`.

---

## 3. Pasang AI Office

```bash
# 1. ekstrak zip ke folder kerja
cd ~
unzip ai-office.zip
cd ai-office

# 2. buat virtual environment Python
python3 -m venv .venv
source .venv/bin/activate        # (Win) .\.venv\Scripts\activate

# 3. pasang dependency
pip install -r requirements.txt

# 4. cek semua file bisa dimuat
python -c "import office, roles, monitor, dashboard; print('ok')"
```

Harus tercetak `ok`. Kalau error `ModuleNotFoundError: claude_agent_sdk`, pastikan `.venv` aktif (ada tulisan `(.venv)` di prompt).

---

## 4. Konfigurasi

```bash
cp .env.example .env
nano .env        # atau editor lain
```

Isi minimal untuk langganan:

```
ANTHROPIC_API_KEY=          <- BIARKAN KOSONG
USE_OPENROUTER=0
```

Batas biaya per tahap (angka estimasi, bukan tagihan) — untuk percobaan pertama turunkan:

```
BUDGET_BA=1.0
BUDGET_DOC=2.0
BUDGET_SA=1.5
BUDGET_DEV=4.0
BUDGET_QA=2.0
MAX_FIX_LOOP=1
```

Perilaku saat kuota habis:

```
QUOTA_WAIT=auto           <- tidur sampai reset lalu lanjut sendiri
QUOTA_WAIT_MAX_HOURS=6    <- lebih dari ini tanya dulu
```

Simpan (`Ctrl+O`, `Enter`, `Ctrl+X`).

### 4a. (Opsional) Telegram

1. Chat ke `@BotFather` di Telegram → `/newbot` → beri nama → salin **token**.
2. Chat ke `@userinfobot` → salin **Id** (angka).
3. Buka chat dengan bot baru tadi, kirim `/start` (wajib, kalau tidak bot tidak bisa mengirim pesan).
4. Isi di `.env`:

```
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

Uji:

```bash
python -c "import monitor; monitor.tg_send('tes dari AI Office')"
```

Harus masuk pesan di Telegram.

### 4b. (Opsional) Tool pentest

```bash
pip install semgrep bandit
```

Tanpa ini pentest tetap jalan tapi hanya review manual oleh AI.

---

## 5. Uji jalan pertama (proyek kecil)

Pilih ide yang sangat kecil untuk melihat konsumsi kuota satu siklus penuh.

**Terminal 1 — dashboard:**

```bash
cd ~/ai-office && source .venv/bin/activate
python dashboard.py
```

Buka `http://localhost:8765` di browser. Kalau ingin buka dari HP, cari IP laptop (`hostname -I`) lalu buka `http://<ip>:8765` dari HP di WiFi yang sama.

**Terminal 2 — pipeline** (disarankan di dalam `tmux` supaya tidak mati saat terminal ditutup):

```bash
sudo apt-get install -y tmux      # sekali saja
tmux new -s office
cd ~/ai-office && source .venv/bin/activate
python office.py "aplikasi todo list sederhana dengan login"
```

Keluar dari tmux tanpa mematikan proses: `Ctrl+B` lalu `D`. Masuk lagi: `tmux attach -t office`.

**(Win)** Tidak ada tmux — buka tab kedua di Windows Terminal, `.\.venv\Scripts\activate`, jalankan perintah yang sama. Jangan tutup tabnya; matikan sleep laptop selama pipeline jalan (Settings → System → Power).

Yang terjadi:

1. Baris pertama harus `Mode autentikasi: langganan Claude (OAuth)`. Kalau tertulis `API key`, hentikan (`Ctrl+C`) dan kosongkan `ANTHROPIC_API_KEY` di `.env`.
2. BA menulis `PRD.md`.
3. SA + Techwriter menulis `BRD.md` → **gate 1**. Buka `workspace/<proyek>/docs/BRD.md` (atau tab BRD di dashboard). Ketik `y` untuk setuju, atau tulis feedback lalu Enter → BRD direvisi dan ditanya lagi. Bisa juga dijawab lewat Telegram.
4. FSD → **gate 2**, sama seperti di atas.
5. SPEC → **gate 3**.
6. Lead memecah tiket, programmer bekerja. Pantau tabel tiket di dashboard.
7. QA + Pentest jalan. Kalau ada temuan → gate: `y` untuk perbaikan otomatis.
8. Selesai. Perintah menjalankan aplikasi ada di `SPEC.md` bagian "Cara menjalankan".

Setelah selesai, cek pemakaian kuota di claude.ai → Settings → Usage. Ini patokan untuk proyek berikutnya.

---

## 6. Menjalankan aplikasi hasil kerja

Buka `workspace/<proyek>/docs/SPEC.md`, bagian **Cara menjalankan**. Umumnya:

```bash
cd workspace/<proyek>/app/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload

# terminal lain
cd workspace/<proyek>/app/frontend
npm install && npm run dev
```

Perintah persisnya mengikuti SPEC, karena stack ditentukan SA.

---

## 7. Operasional sehari-hari

**Proses putus (kuota habis, Ctrl+C, PC mati):**

```bash
python office.py --project <nama-proyek>
```

Tahap dibaca otomatis dari `docs/STATE.txt`, sesi Claude Code dilanjutkan dari `docs/SESSIONS.json`.

**Paksa mulai dari tahap tertentu** (`ba|brd|fsd|sa|dev|qa`):

```bash
python office.py --project <nama-proyek> --resume dev
```

**Tambah fitur / ubah proses bisnis pada proyek yang sudah jadi:**

```bash
python office.py --project <nama-proyek> --change "tambah export laporan ke Excel"
```

Semua dokumen ditambah bagian `[v1.1]`, lewat 3 gate yang sama, programmer hanya kerjakan delta.

**Nama proyek** = folder di `workspace/`. Lihat dengan `ls workspace/`.

---

## 7b. Mode hybrid BMAD (opsional)

Kalau ingin dokumen dan dev memakai BMAD-METHOD:

```bash
source .venv/bin/activate
pip install uv                                   # sekali; wajib untuk skill BMAD
python office_bmad.py "aplikasi todo list sederhana dengan login"
```

Run pertama akan memasang BMAD ke `workspace/<proyek>/` (butuh internet, ~1 menit). Gate-nya:
1. PRD BMAD selesai → Bos lihat asumsi dan pertanyaan terbuka; jawab lewat terminal/Telegram → PRD diupdate → ulangi sampai `y`.
2. Architecture, sama.
3. `stories.yaml` → daftar story ditampilkan; `y` atau tulis perubahan.
4. Tiap story dikerjakan `bmad-build-auto`. Kalau BLOCKED, Bos ditanya.
5. QA + Pentest → FIXES → perbaikan otomatis.

Lanjutkan / change request sama seperti `office.py`, hanya nama skripnya `office_bmad.py`.

## 8. Mengubah perilaku peran

| Ingin mengubah | File |
|---|---|
| Cara kerja/isi dokumen tiap peran | `prompts/<peran>.md` |
| Model per peran (opus/sonnet/haiku) | `roles.py` |
| Perintah shell yang dilarang | `roles.py` → `DENY_BASH` |
| Batas turn per subagent | `roles.py` → `maxTurns` |
| Batas biaya per tahap | `.env` |

Tidak perlu restart apa pun; perubahan dibaca saat pipeline berikutnya dijalankan.

---

## 9. Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| `Claude Code CLI tidak ditemukan di PATH` | `npm install -g @anthropic-ai/claude-code`, lalu buka terminal baru |
| `Mode autentikasi: API key` padahal mau langganan | Kosongkan `ANTHROPIC_API_KEY` di `.env` dan di environment shell (`unset ANTHROPIC_API_KEY`) |
| Error 401 / minta login | `claude` → `/login` ulang |
| Tahap berhenti dengan `rate_limit` | Kuota habis. Biarkan (`QUOTA_WAIT=auto`) atau jalankan lagi nanti dengan `--project` |
| Tahap berhenti dengan `error_max_budget_usd` | Naikkan `BUDGET_*` yang bersangkutan di `.env`, jalankan `--project` lagi |
| Dashboard kosong / "belum ada proyek" | Belum ada folder di `workspace/`; jalankan pipeline dulu |
| Dashboard tidak bisa dibuka dari HP | Firewall laptop memblokir port 8765; buka port atau pakai `localhost` saja |
| Telegram tidak menerima pesan | Belum kirim `/start` ke bot, atau `TELEGRAM_CHAT_ID` salah |
| Tiket tidak muncul di dashboard | Lead menulis `TASKS.md` tidak sesuai format tabel; buka file-nya dan cek kolom `Status` |
| Programmer saling timpa file | Pemecahan tiket kurang bagus. Tambahkan di `prompts/lead.md`: "maksimal 2 programmer bersamaan" |
| (Win) Programmer memakai perintah PowerShell / larangan `rm -rf` tidak jalan | Git for Windows belum terpasang, Claude Code jatuh ke PowerShell. Pasang Git for Windows, restart terminal |
| (Win) `semgrep` gagal dipasang | Lewati; pentest tetap jalan dengan `bandit` + review manual. Atau pakai WSL2 |
| Resume sesi bertingkah aneh | Hapus baris tahap itu di `docs/SESSIONS.json`, jalankan `--project` lagi (jatuh ke sesi baru) |
| Pemakaian tercatat sebagai API padahal login langganan | Akun Bos juga terdaftar di organisasi API. Cek claude.ai Usage vs platform.claude.com; kalau salah masuk, `/logout` lalu `/login` ulang pilih subscription |

---

## 10. Keamanan minimum untuk PoC

- Programmer punya akses Bash ke laptop (dengan daftar larangan). Jangan jalankan di folder yang berisi data penting; folder `workspace/` cukup terisolasi tapi bukan sandbox.
- Jangan commit `.env` (sudah ada di `.gitignore`).
- Dashboard tidak punya login. Jangan buka portnya ke internet.
- Kalau nanti dipakai lebih serius: jalankan seluruh folder di container Docker, satu git worktree per tiket, dan pindah ke API key dengan `max_budget_usd` sebagai rem biaya sungguhan.
