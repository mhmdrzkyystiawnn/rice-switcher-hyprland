# Design — Onboarding TUI: clone, auto-manifest, gap detection

**Status:** draft **revisi #6** — Prasyarat 0 (kill-by-identity), **Fase 1 (clone + naming)**, **Fase 2 (detect + tomlwrite + `generate`) + penyempurnaan 2b**, **Fase 3a (review/edit via prompt baris-per-baris + simpan non-interaktif)**, **Fase 4 sempit (deteksi gap autostart bentrok + needs-materialization, penanganan di review, `--yes` berhenti) — tanpa eksekusi skrip**, dan **Fase 5 versi standalone (`clean-backups`: hapus `.bak-*` dengan aman, retensi, penjaga lock switch, audit)** sudah diimplementasikan & diuji; Fase 3b & 6 belum.
**Status implementasi:** ✅ Prasyarat 0 (§12) — `process.py`/`lifecycle.py`/`managed_set.py`, identitas tanpa argumen (`exact_argv`, Fase 2b) — `tests/test_slice0_exact_identity.py`; ✅ Fase 1 clone + naming (§3, §9, §10) — `rice_switcher/clone.py`, subcommand `clone`, `tests/test_clone.py`; ✅ Fase 2 deteksi & draft manifest (§4) — `rice_switcher/detect.py`, `rice_switcher/tomlwrite.py`, subcommand `generate`, `tests/test_detect.py`, `tests/test_detect_reachability.py`, `tests/test_tomlwrite.py`, `tests/test_generate.py`; ✅ integrasi end-to-end `clone → generate → validate → switch` — `tests/test_e2e_onboarding.py`; ✅ peringatan `validate` untuk `stop` no-op — `tests/test_slice2b_stop_noop.py`; ✅ Fase 3a review/edit & simpan tanpa curses (§5) — `rice_switcher/review.py`, `rice_switcher/review_cli.py`, `generate --no-tui/--yes`, `tests/test_slice3a_review.py`, `tests/test_slice3a_review_cli.py`, `tests/test_slice3a_generate_review.py`, `tests/test_slice3a_client_cmd.py`; ✅ Fase 4 sempit deteksi gap + `started_by_config` (§6, §4.1/§4.2) — `rice_switcher/gaps.py`, field `Component.started_by_config`, `compute_plan.to_config`, perintah review `c`, `generate --yes` berhenti pada temuan tak tertangani, `tests/test_slice4a_started_by_config.py`, `tests/test_slice4_gaps.py`, `tests/test_slice4_detect_findings.py`, `tests/test_slice4_review_findings.py`, `tests/test_slice4_e2e.py`; ✅ Fase 5 **standalone** — `rice_switcher/backups.py` (list/plan/hapus aman, no-follow), subcommand `clean-backups [--keep N] [--yes]`, penjaga `switch_lock`, audit `{"action":"cleanup",…}`, `tests/test_slice5_backups.py`, `tests/test_slice5_clean_backups.py`, `tests/test_slice5_e2e.py`; ⬜ Fase 3b & 6 (prompt cleanup di dalam `switch`/`onboard` menunggu façade).
**Cakupan:** alur "clone rice orang lain → bisa di-`switch`" menjadi semi-otomatis lewat TUI.
**Dokumen terkait:** `docs/PLAN.md` (spec inti switch; §2c managed set, §2f `config_link`, §2g identitas proses, §2h keamanan, §4.1 skema manifest, §5.1 zero-dep, §6 roadmap), `README.md`.
**Sifat:** dokumen desain. Tidak mengubah `docs/PLAN.md`; mekanisme inti switch tetap apa adanya — **kecuali** Prasyarat 0 (kill-by-identity, §12) yang menyentuh `process.py`/`lifecycle.py`.

**Riwayat revisi**
- **revisi #6** — **Fase 5 versi standalone** selesai: modul murni `rice_switcher/backups.py` (`list_backups` cocok **persis** pola `_now_ts()` sebagai `<name>.bak-<8digits>T<6digits>_<3digits>`, urut lewat **nama** bukan mtime, `lstat` tanpa mengikuti symlink; `plan_cleanup(entries, keep)`; `remove_backup` dengan containment `os.path.commonpath` pada **induk** yang di-`realpath`, symlink → `os.unlink`, direktori nyata → `shutil.rmtree`, jenis lain ditolak) dan subcommand `rice-switcher clean-backups [--keep N] [--yes]` (`rice_switcher/cli.py::cmd_clean_backups`). Menolak berjalan bila `switch_lock` sedang dipegang (switch/rollback berlangsung); menampilkan daftar + konsekuensi (rollback jalur cepat hilang, tinggal snapshot); default tanya = **N**, EOF/kosong = batal; kegagalan satu entri tidak menghentikan yang lain (exit non-nol); audit `{"action":"cleanup","removed":[…],"failed":[…]}`. `tests/test_slice5_backups.py`, `tests/test_slice5_clean_backups.py`, `tests/test_slice5_e2e.py`. **Prompt di dalam `switch`/`onboard` belum** (menunggu façade `onboard`).
- **revisi #5** — **Fase 4 sempit** selesai (deteksi gap + penanganan, **tanpa eksekusi**): modul murni `rice_switcher/gaps.py` (temuan `autostart_conflict` + `needs_materialization`, baca-saja, containment + `reachable`), field manifest eksplisit **`started_by_config`** (default `False`, pola `exact_argv`; `compute_plan` mengeluarkannya dari `to_start` → daftar `to_config` "dijalankan oleh config"; `validate` menolaknya untuk compositor), temuan terstruktur di `ManifestDraft.findings`, driver review menampilkan temuan + perintah **`c`** (opsi default / opsi lain / skip, dicatat di riwayat, bisa di-`undo`), dan `generate --yes` menerapkan opsi default lalu **berhenti** pada temuan tanpa default. **Keputusan Langkah 0 dicatat** (§6.0). Opsi A (netralkan baris) **belum** diimplementasikan — hanya dilaporkan. `tests/test_slice4a_started_by_config.py`, `tests/test_slice4_gaps.py`, `tests/test_slice4_detect_findings.py`, `tests/test_slice4_review_findings.py`, `tests/test_slice4_e2e.py`.
- **revisi #4** — Fase 3a selesai: model review murni (`rice_switcher/review.py`) + driver prompt baris-per-baris (`rice_switcher/review_cli.py`) + jalur simpan non-interaktif `generate --no-tui/--yes` (§5). Perintah klien sekali-jalan (`swww restore`, `swww img …`) ditandai `ask`, bukan `auto` (§4.1, Langkah 0). `ManifestDraft` membawa `findings` (kosong; Fase 4 mengisi).
- **revisi #3** — penyempurnaan Fase 2b pasca-implementasi: (1) deteksi berbasis **reachability** dari entrypoint (§4.1), (2) pemecahan **perintah gabungan** + pelewatan wrapper, (3) tabel executable diperluas (`awww`/`swww-daemon`, `pypr`, `mpvpaper`, dst.), (4) **identitas tanpa argumen** `exact_argv` untuk daemon (§4.2), (5) peringatan `validate` untuk `stop` no-op, (6) koreksi dokumen §4.3/§4.5/§4.6 agar cocok dengan implementasi & tes.
- **revisi #2** — menutup review keamanan & desain: (1) hardening argument injection saat clone, (2) larangan nama berawalan titik, (3) aturan containment symlink, (4) provenance di file terpisah + URL di-redact, (5) temuan baru **autostart bentrok**, (6) cleanup `.bak-*` via `lstat`+`unlink` (bukan `rmtree`), (7) tampilkan isi skrip sebelum approve. Plus **keputusan 7 pertanyaan terbuka** (§14) dan **Prasyarat 0: kill-by-identity** di core (§12). Urutan dibalik: fallback prompt dulu, `curses` terakhir (§5.1).
- revisi #1 — draft awal.

---

## 0. Ringkasan feasibility (dasar desain)

| # | Pertanyaan | Verdict | Sifat |
|---|---|---|---|
| 1 | Clone repo otomatis via TUI (input URL) | **BISA** | penuh |
| 2 | Taruh & namai otomatis, default dari repo | **BISA** | penuh |
| 3 | Analisis & tulis `rice.toml` otomatis **tanpa** user | **SEBAGIAN** | draft auto; final butuh review |
| 4 | Gap/prasyarat rice | **pola bisa dikenali**, eksekusi otomatis **TIDAK** | deteksi auto + approve user |
| 5 | Alur keseluruhan | **SEMI-OTOMATIS** | auto-draft + review TUI |
| 6 | TUI tanya hapus backup | **BISA** | penuh |
| 7 | Monitor/resolusi | **BISA** | auto-deteksi + override user |

Dokumen ini merancang tepat sebatas batas itu: yang **BISA** dibuat penuh, dan yang **SEBAGIAN** dibuat auto-draft + review.

---

## 1. Prinsip & batasan (hard constraints)

1. **Zero runtime dependency (stdlib only).** `pyproject.toml:8-9` + `docs/PLAN.md §5.1`. Karena itu:
   - TUI memakai **`curses`** (stdlib), **bukan** `textual`/`rich`/`prompt_toolkit`.
   - Serialisasi TOML ditulis sendiri (`tomllib` **read-only**, tidak bisa menulis). Lihat §4.5.
2. **Review-first, bukan blind-auto.** Generator hanya menghasilkan **draft**. User melihat & mengedit di TUI sebelum disimpan. `validate` (`validate_manifest`), `switch` default **dry-run**, dan konfirmasi `yes` untuk rice hash baru tetap menjadi gerbang.
3. **No RCE.** Tidak pernah menjalankan script pihak ketiga (`install.sh`, `*-profile.sh apply`, dsb.) secara otomatis. Setiap langkah berisiko harus **disetujui user per item** dan dicatat di audit. Tidak ada `eval`/`shell=True` atas input repo.
4. **Tidak menyentuh live desktop** sampai user benar-benar menjalankan `switch`. Onboarding hanya menulis di `<RICE_HOME>` (+ `state.json` bila `adopt`).
5. **Selalu ada jalur non-interaktif.** Setiap langkah TUI punya padanan flag (`--url`, `--name`, `--no-tui`, `--yes`, `--monitor`, `--keep-backups N`) agar bisa diuji hermetis dan dipakai dari skrip/keybind.
6. **Audit** semua aksi (clone, generate, hook-approve, monitor-patch, cleanup) mengikuti `docs/PLAN.md §2h`.
7. **Determinisme di tes.** Subprocess (`git`, `hyprctl`) injectable seperti pola `runner`/`probe`/`spawner` yang sudah ada.
8. **Input repo asing tidak dipercaya (hardening).** URL ditolak bila diawali `-` (cegah argument injection); clone memakai separators `git clone -- <url> <dir>`; `GIT_TERMINAL_PROMPT=0` (repo privat tidak boleh hang menunggu kredensial); protokol dibatasi (blokir `ext::`). Tidak pernah mengikuti symlink yang `realpath`-nya keluar folder rice (containment). Detail: §3.2, §4.3, §10.
9. **Tidak ada eksekusi/traversal buta.** Setiap langkah prasyarat pihak ketiga menampilkan isi skripnya (atau path + opsi buka pager) sebelum approve; tidak ada `rmtree` yang mengikuti symlink.

---

## 2. Arsitektur

### 2.1 Modul baru

| Modul | Tanggung jawab |
|---|---|
| `rice_switcher/clone.py` | `derive_name(url)`, `do_clone(url, name, roots, *, runner, depth, branch)`. Pure-ish, injectable runner. |
| `rice_switcher/detect.py` | Analisis direktori rice → `ManifestDraft` (komponen + `config_link` + catatan). Murni baca FS. |
| `rice_switcher/tomlwrite.py` | Serializer TOML minimal untuk bentuk manifest (scalar → tabel → array-of-tables, sesuai `docs/PLAN.md §4.1`). |
| `rice_switcher/monitor.py` | `detect_monitors(hyprctl_json)` + `patch_monitor_lines(text, primary)`; injectable `hyprctl`. |
| `rice_switcher/tui.py` | Layar `curses`: input, review/edit komponen, gap, preview plan, konfirmasi, cleanup. Tipis; logika di modul lain. |
| `rice_switcher/onboard.py` | Orkestrator: clone → detect → review → gap → validate → (opsional) switch → cleanup. |

### 2.2 Perubahan modul lama

- `rice_switcher/cli.py`: subcommand `onboard`, `clone`, `generate`, `clean-backups` (lihat §2.4).
- `rice_switcher/process.py` + `rice_switcher/lifecycle.py`: **Prasyarat 0** — kill-by-identity (SIGTERM ke PID hasil `find_pids_by_identity`), dipakai saat `stop` kosong (§4.2, §12). Saat ini `process.py` sengaja read-only (`"Slice 1: never"`, `process.py:7-14`) dan `stop_component` dengan `stop` kosong hanya menjalankan `true` lalu menunggu → timeout (`lifecycle.py:119-136`). Perubahan ini diisolasi + TDD, tidak mengubah perilaku rice yang sudah punya `stop`.
- `rice_switcher/paths.py`: `validate_rice_name` ditambah aturan **tidak boleh diawali titik** — `.`/`..` **sudah** ditolak (`paths.py:40-41`), tetapi regex `^[A-Za-z0-9._-]{1,64}$` (`paths.py:28`) masih meloloskan `.foo`; helper `sanitize_name` untuk `derive_name`.
- `rice_switcher/validate.py`: rule "nama tidak berawalan titik"; opsional peringatan layout `.config` (§4.4).
- `rice_switcher/manifest.py`: **tidak** menambah field provenance — provenance dipindah ke file terpisah `.rice-source.toml` (§9).

### 2.3 Alur end-to-end

```
   ┌──────────┐   ┌───────────┐   ┌──────────────┐   ┌──────────────┐
   │ TUI: URL │──▶│ clone.py  │──▶│ detect.py    │──▶│ TUI: review  │
   └──────────┘   │ git clone │   │ → draft      │   │ /edit draft  │
                  └───────────┘   └──────────────┘   └──────┬───────┘
   ┌───────────────┐                                        │
   │ TUI: name     │  (default = basename URL)              ▼
   │ (default ok)  │                             ┌────────────────────┐
   └───────┬───────┘                             │ tomlwrite → save   │
           │                                     └─────────┬──────────┘
           │                                               ▼
           │                              ┌──────────────────────────────┐
           │                              │ detect gap → TUI approve/edit│
           │                              └───────────────┬──────────────┘
           │                                              ▼
           │                                  ┌───────────────────────┐
           │                                  │ validate + dry-run    │
           │                                  └───────────┬───────────┘
           │                                              ▼
           │                                  ┌───────────────────────┐
           └─────────────────────────────────▶│ TUI: switch? (opsi)   │
                                              └───────────┬───────────┘
                                                          ▼
                                              ┌───────────────────────┐
                                              │ switch + cleanup bak? │
                                              └───────────────────────┘
```

### 2.4 Permukaan CLI

| Perintah | Fungsi | Interaktif? |
|---|---|---|
| `rice-switcher onboard [--url U] [--name N] [--no-tui] [--yes] [--switch] [--monitor M] [--keep-backups N]` | Alur penuh (clone→draft→review→gap→validate→opsional switch→opsional cleanup). | ya (TUI) / tidak (`--no-tui`) |
| `rice-switcher clone <url> [--name N] [--depth D] [--branch B]` | Hanya clone + naming (hardened, §3.2). | tidak |
| `rice-switcher generate <name> [--write] [--no-tui]` | Analisis rice yang sudah ada → draft/langsung tulis. | ya/tidak |
| `rice-switcher clean-backups [--keep N] [--yes]` | Hapus `.bak-*` dengan aman (§7). | ya/tidak |

`onboard` adalah façade; `clone`/`generate` adalah pecahannya (agar bisa dites & dipakai skrip).

---

## 3. Fase 1 — `clone` + naming (BISA, penuh)

### 3.1 Naming
- `derive_name(url)`:
  1. buang query/fragment, buang trailing `/`;
  2. ambil basename path; buang akhiran `.git`;
  3. buang titik di awal (`.foo` → `foo`), lalu sanitasi ke `[A-Za-z0-9._-]{1,64}` (regex `_RICE_NAME_RE`, `paths.py:28`);
  4. kalau hasil kosong/tak valid → fallback `"rice"`.
- **Aturan tambahan: nama tidak boleh diawali titik.** `validate_rice_name` **sudah** menolak `.` dan `..` (`paths.py:40-41`), tetapi `_RICE_NAME_RE` (`paths.py:28`) masih meloloskan `.foo` — padahal temp dir memakai `.<name>.tmp-<pid>` sehingga bisa bentrok. Tambahkan penolakan berawalan titik di `validate_rice_name` (bukan hanya di sanitizer), plus tesnya.
- Default di TUI ditampilkan; user boleh menimpa; jika input kosong → pakai default (sesuai permintaan poin 2).
- Validasi akhir tetap lewat `validate_rice_name` (`paths.py:31-51`).
- **Konsistensi wajib:** `[meta].name` yang ditulis generator = nama folder yang dipilih (karena `switch` me-resolve target dari `meta.name`, `switch.py:176`).

### 3.2 Eksekusi clone (hardened)

**Validasi URL sebelum clone:**
- **Tolak** URL yang diawali `-` (mis. `--upload-pack=…`) — `git` akan membacanya sebagai opsi → argument injection.
- **Batasi protokol**: default aman = **`https://`** dan **`ssh://`** (+ bentuk scp `git@host:path`). **`http://`/`git://` tidak terenkripsi** → tetap diizinkan tapi **beri peringatan + konfirmasi**. **Blokir** `ext::` dan skema lain (`file://` hanya dengan `--allow-local` eksplisit, untuk tes).
- Tolak URL yang mengandung karakter kontrol/newline.

**Eksekusi:**
- Argv: `git clone [--depth D] [--branch B] -- <url> <RICE_HOME>/<name>` (separator `--` didukung `git clone`; lihat `git clone --help`).
- Environment: `GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS=echo` agar repo privat **gagal cepat**, bukan hang menunggu kredensial.
- Kasus yang ditangani: `git` tak ada (rc 127), jaringan gagal, repo privat/auth, repo kosong, URL invalid, `<name>` sudah ada (prompt **rename** atau **batal** — default batal, tidak menimpa), disk penuh.
- Atomicity: clone ke direktori sementara `.<name>.tmp-<pid>` lalu `os.rename` ke final; gagal → bersihkan temp (aturan no-follow, §10).
- Audit `{"action":"clone","url":<redacted>,"name":...,"rc":...}` (URL bisa mengandung token → redact, §9/§10).
- Progres: TUI menampilkan spinner teks; non-TUI mencetak ringkas.

### 3.3 Acceptance (Fase 1)
- Tes hermetis: `git` distub via PATH; `derive_name` untuk ~10 bentuk URL (termasuk `…/repo.git`, trailing `/`).
- **Keamanan**: URL diawali `-` ditolak; `ext::` ditolak; argv ke `git` memakai `--`; env `GIT_TERMINAL_PROMPT=0` diset; nama berawalan titik ditolak (`validate_rice_name`).
- Kasus dir sudah ada; kegagalan clone → temp dibersihkan & rc≠0.
- Tidak ada tulisan ke `~/.config`/`~/.local`.

---

## 4. Fase 2 — Deteksi & draft manifest (SEBAGIAN)

Inti fitur. Menghasilkan `ManifestDraft` (bukan final). Sumber inferensi utama = **isi config rice itu sendiri**, terutama entri autostart-nya.

### 4.1 Signal → inferensi (heuristik)

| Signal di repo | Inferensi | Keyakinan |
|---|---|---|
| `.config/hypr/**`, `hypr/**`, `hyprland.conf`, `hyprland.lua` | komponen `hyprland`, `type=compositor`, `reload="hyprctl reload"`, `config_link=~/.config/hypr` | tinggi |
| `waybar/config*`, `waybar/style.css` | `waybar`; `start="waybar"`, `start_check="pgrep -x waybar"`; `stop` **kosong** (§4.2) | tinggi |
| `quickshell/<cfg>/shell.qml` | `quickshell`; `start="qs -c <cfg>"`, `argv_match=["-c","<cfg>"]`, `executable="qs"`; `stop` **kosong** (§4.2) | tinggi |
| `hypridle.conf` | `hypridle`; `start="hypridle"`; `stop` **kosong** (§4.2) | tinggi |
| dir/config `dunst`, `mako`, `swaync` | daemon notifikasi | sedang |
| `hyprpaper.conf`, ref `swww`/`awww`/`mpvpaper` | wallpaper daemon | sedang |
| **Entri autostart Hyprland**: `exec-once = …`, `exec = …`, `hl.exec_cmd("…")`, `hl.on("hyprland.start", …)` | kandidat `start` + `executable` + kadang `argv_match` | **tinggi** (sumber terkuat) |
| file variabel (`variables.lua`, `*.conf` `$var=`) | resolusi `$qsConfig` dsb. → nilai konkret | tinggi |
| README "Dependencies"/`packages` | daftar binary untuk cek (`validate`) | sedang |

Contoh pola (teruji di `tests/test_detect.py`): `hl.exec_cmd("qs -c $qsConfig")` + `hl.env("qsConfig","ii")` ⇒ komponen quickshell `qs -c ii`. Parser harus mengikuti indireksi variabel ini (variabel hanya diambil dari berkas yang terjangkau — lihat di bawah).

**Reachability (Fase 2b).** Hanya autostart dari berkas yang benar-benar **terjangkau** dari entrypoint (`hyprland.lua`/`.conf`) yang menjadi komponen. Detector mengikuti `source =` (hyprlang) dan `require(...)`/`dofile(...)` literal (Lua) selama `realpath` tetap di dalam folder rice (containment), dengan penjaga siklus + batas kedalaman (`MAX_SOURCE_DEPTH`). Direktori `archive`/`old`/`backup`/`bak`/`examples`/`example` dan profil tak aktif tidak pernah dianggap hidup; ref-nya tetap dicatat dengan `reachable=False` (untuk Fase 4). Bila tidak ada entrypoint yang terbaca, detector **tidak** mundur ke "pindai semua" — dilaporkan sebagai catatan "tidak ada entrypoint". Perintah gabungan (`&&`, `;`, `||`, `|`, `&`) dipecah (menghormati quote) dan wrapper (`sleep N`, `env VAR=...`, `exec`, `setsid`, `nohup`, `uwsm app --`, `systemd-run --user`) dilewati sebelum mengambil executable. Tabel executable: `qs`/`quickshell`, `waybar`, `hypridle`, `dunst`, `mako`, `swaync`/`swaync-client`, `hyprpaper`, `swww`/`swww-daemon`, `awww`/`awww-daemon`, `mpvpaper`, `hyprlock`, `hyprsunset`, `hyprpolkitagent`, `pypr`, `fcitx5`. Deteksi ini memakai mekanisme identitas Fase 2b: perintah tanpa argumen ⇒ komponen dengan `exact_argv = true` (bisa dihentikan), perintah dengan argumen literal ⇒ `argv_match` (subset). Perintah **klien sekali-jalan** yang dikenal (`swww restore`, `swww img …`, dan padanan `awww`) **tidak** dianggap daemon: identitasnya ditandai `ask` (+ catatan "perintah klien, mungkin bukan daemon"), sedangkan `swww-daemon` tetap `auto` (§5, Langkah 0).

**⚠ Autostart bentrok (Fase 4 — wajib, lihat §6.0/§6.1).** Baris autostart itu **tetap ada** di config rice. Kalau detector menjadikannya komponen terkelola, `switch` akan **start** komponen itu, sementara `exec-once`/`exec` di config juga menjalankannya → **start ganda**, atau **saling bunuh** saat reload (`exec` jalan ulang tiap `hyprctl reload`; `exec-once` hanya sekali). Karena itu detector memunculkan temuan terstruktur **`autostart_conflict`** dengan dua opsi penanganan (§6.0/§6.1). **Default per jenis (keputusan Langkah 0):** baris `exec` (dijalankan ulang tiap reload) → **Opsi B** (`started_by_config = true`; switcher hanya stop); baris `exec-once` dan `hl.exec_cmd` di dalam `hl.on("hyprland.start")` (hanya sekali saat login) → **`ask`** (tanpa default ← keyakinan rendah, `--yes` berhenti). Komponen dari satu baris `exec` **tidak** diam-diam dibuat start+stop; keputusan itu eksplisit.

### 4.2 Yang butuh keputusan / perhatian user
- **`stop` → dikosongkan untuk komponen terkelola (keputusan #2).** Skema manifest (`docs/PLAN.md §2.0`) punya default `stop = "true"` (no-op), tetapi generator **selalu menulis `stop = ""` secara eksplisit** untuk komponen non-compositor agar memilih jalur stop berbasis identitas, bukan no-op. Kompositor dikecualikan: `stop = "true"` (switcher tidak pernah menghentikan/menjalankan kompositor, `docs/PLAN.md §2c`). `stop` kosong → `stop_component` memakai stop bawaan berbasis PID (identitas `executable` + `argv_match`/`exact_argv`, `docs/PLAN.md §2g`). Ini bergantung pada **Prasyarat 0** (§12): kill-by-identity di `process.py`/`lifecycle.py`. Kalau identitas ambigu → tandai `ask`, jangan mengarang `stop`.
- `argv_match` bila ambigu (variabel tak teresolusi, quoting, beberapa instance).
- `type` bila ambigu ("shell" vs "bar").
- `name` komponen bila bentrok.
- **Urutan** start antar komponen.

**Identitas tanpa argumen (`exact_argv`, Fase 2b).** Daemon tanpa argumen (mis. `waybar`, `hypridle`, `mako`, `dunst`, `awww-daemon`) tidak punya token `argv_match`; subset match `argv_match=[]` terlalu luas (cocok dengan `waybar -c x` juga). Karena itu manifest punya field eksplisit `exact_argv = true`: argv proses harus **sama persis** dengan `executable` + `argv_match` (jumlah, isi, urutan). `exact_argv = true` + `argv_match = []` = identitas "tanpa argumen" (argv proses tepat satu elemen). `argv_match = []` **tanpa** `exact_argv` tetap **ditolak** (kill basename-only dilarang) dengan pesan yang menyebut `exact_argv`. Generator menulis `exact_argv = true` untuk daemon tanpa argumen sehingga draf bisa langsung di-`switch` (teruji end-to-end: `tests/test_e2e_onboarding.py`). `validate` memperingatkan komponen non-compositor dengan `start` bermakna tetapi `stop` no-op (`true`/`:` atau `stop` kosong tanpa identitas) — "tidak akan pernah dihentikan; switch akan start ganda".

**Dijalankan oleh config (`started_by_config`, Fase 4).** Field boolean opsional, **default `False`**, pola sama dengan `exact_argv`. Bila `true`, artinya **config rice sendiri** yang menjalankan komponen ini (baris `exec` yang jalan ulang tiap `hyprctl reload`), sehingga `compute_plan` **mengeluarkan identitas itu dari `to_start`** (switcher tidak pernah start → tidak ada start ganda) tetapi **tetap boleh `to_stop`** (switcher menghentikannya sebelum pindah rice). Rencana menampilkannya di daftar **`to_config`** dengan label "dijalankan oleh config". `started_by_config` **tidak** mengubah identitas/`Component` posisional lama dan manifest lama parse identik (`False`). `validate` **menolaknya untuk `type = "compositor"`** (compositor tidak pernah di-start/di-stop oleh switcher). Field ini diisi oleh review (perintah `c`) atau otomatis oleh `generate --yes` untuk temuan `autostart_conflict` ber-`exec`.

Draft menandai tiap field dengan tingkat keyakinan (`auto` / `guess` / `ask`). TUI menonjolkan yang `guess`/`ask`.

**Guard kill-by-identity (brief Prasyarat 0).** Ini kode baru yang bisa **membunuh proses**, dan `executable`/`argv_match` bisa berasal dari `rice.toml` orang lain. Wajib:
- Hanya PID milik **uid sendiri**; abaikan **PID 1**, PID switcher sendiri, dan **parent**-nya.
- Cocokkan **argv lengkap** (identitas `executable` + `argv_match`/`exact_argv`), **bukan** basename saja — kalau tidak, `qs -c ii` dan `qs -c minimal` dianggap sama. Tolak/lewati bila `argv_match` kosong **dan** `exact_argv` tidak di-set (terlalu luas); `exact_argv = true` + `argv_match = []` sah untuk daemon tanpa argumen (argv harus tepat satu elemen).
- **Baca ulang `/proc/<pid>/cmdline` tepat sebelum kill** (cegah PID reuse).
- **SIGTERM → tunggu (timeout) → SIGKILL**.
- **Lindungi proses sesi**: manifest dengan `executable` = `Hyprland`/`systemd`/`dbus-daemon`/`gnome-keyring-daemon` (dan sejenisnya) **ditolak validator** — ada tesnya.
- **Gerbang konfirmasi menampilkan target**: saat ini konfirmasi `yes` untuk rice baru menampilkan command string, padahal kill-by-identity bukan string → tambahkan baris **"akan menghentikan proses: …"**.

### 4.3 Layout → `config_link` (keputusan #1)
Konvensi swap: target = `<RICE_HOME>/<name>/<basename(config_link)>` (`switch.py:279-285`). Artinya config harus ada di **root rice** dengan nama `basename`-nya.

Dua bentuk repo:
- **(a) Gaya `~/.config`**: repo punya `.config/hypr`, `.config/waybar`, … → generator memetakan `config_link="~/.config/hypr"`, tetapi **harus menormalkan layout** ke root rice.
- **(b) Gaya polos**: repo punya `hypr/`, `waybar/`, … langsung di root → langsung cocok.

**Keputusan:** normalisasi ke root rice dengan **symlink relatif** (`<name>/hypr -> .config/hypr`) supaya tidak menggandakan file besar. Symlink yang dibuat generator **didaftarkan di `<rice>/.git/info/exclude`** agar repo clone tetap bersih dan `git pull` tidak bentrok.

**Containment wajib:** saat scan (`detect.py`) maupun saat membuat/menghapus symlink normalisasi, **jangan mengikuti** symlink yang `realpath`-nya keluar dari folder rice. Repo asing bisa berisi `hypr -> /etc` atau `waybar -> ~/.ssh`; tanpa aturan ini, isi pribadimu bisa terbaca atau ikut ter-swap. Detail: §10.

**Teruji end-to-end (Fase 2b Langkah 4).** `tests/test_e2e_onboarding.py` menjalankan `clone` (repo `file://` lokal) → `generate --write` → `validate` → `switch` hermetic di tempdir (`RICE_HOME`/`RICE_CONFIG_HOME`/`RICE_STATE_HOME` + `HOME` di-override) dan membuktikan temuan #6: symlink normalisasi `<rice>/<x> -> .config/<x>` membuat target swap `<RICE_HOME>/<name>/<x>` (yang dihitung `switch.py`) resolve ke `<rice>/.config/<x>`, sehingga symlink live `~/.config/<x>` menunjuk ke pohon yang benar.

### 4.4 Anomali yang dideteksi (bukan gap, tapi diperingatkan)
- Tak ada config yang dikenal → "rice ini mungkin bukan rice Hyprland".
- `config_link` bentrok dengan rice lain (dicek `validate`).
- **Symlink keluar folder rice** (`realpath` di luar) → ditolak/diabaikan + peringatan (§10).
- Binary hilang (mis. `pypr`, `wal`) → warning (bukan error), konsisten `validate`.

### 4.5 `tomlwrite.py`
- `tomllib` hanya baca → serializer minimal menulis: string, int, bool, array of string, tabel, array-of-tables.
- Urutan key mengikuti `docs/PLAN.md §4.1`: **scalar → tabel → array-of-tables**.
- Tes: round-trip `tomllib.loads(tomlwrite(draft)) == draft` pada manifest contoh (termasuk kasus array-of-tables dengan komentar). Catatan: `fixtures/**` **tidak** berisi `rice.toml` — hanya config pihak ketiga (mis. `matugen/config.toml`, `fish/starship.toml`) — jadi round-trip diuji pada manifest sintetis + draf hasil `detect`, bukan pada `fixtures/**`.

### 4.6 Acceptance (Fase 2)
- `detect` pada rice yang dibangun dari fixture (`fixtures/hyprland.lua` + `fixtures/hyprland`, `fixtures/waybar`, `fixtures/quickshell`) menghasilkan komponen yang diharapkan. `fixtures/illogical-impulse` adalah **direktori data** (berisi `actions/`, `config.json`), bukan sinyal komponen, dan tidak dipakai sebagai fixture deteksi.
- Autostart dari berkas **tak terjangkau** (arsip/profil/backup) diabaikan; hanya berkas terjangkau dari entrypoint yang menghasilkan komponen.
- Tidak ada entrypoint terbaca ⇒ dilaporkan ("tidak ada entrypoint"), **bukan** fallback pindai-semua.
- Perintah gabungan dipecah dan wrapper dilewati; identitas diturunkan dari argv literal (tanpa argumen ⇒ `exact_argv = true`).
- Field yang ambigu ditandai `ask` (bukan dipaksakan).
- Round-trip TOML hijau.

---

## 5. Fase 3 — TUI review/edit (BISA) — ✅ 3a (prompt baris-per-baris); 3b (`curses`) belum

### 5.1 Teknologi & urutan pembangunan
- **Prompt baris-per-baris dibangun LEBIH DULU; `curses` paling akhir** (urutan review #1). Jalur non-interaktif memang wajib (`--no-tui`/non-TTY), jadi TUI hanya pembungkus tipis di atasnya. Dengan ini Fase 1–2 (clone + detect + tomlwrite) sudah berguna tanpa menunggu TUI. ✅ **Fase 3a** sudah mengimplementasikan jalur ini (`rice_switcher/review.py` + `rice_switcher/review_cli.py`); `curses` (Fase 3b) belum.
- **`curses` (stdlib)** untuk TUI. Alasan: zero-dep (`docs/PLAN.md §5.1`). Bila `stdout` bukan TTY atau `--no-tui` → pakai prompt baris-per-baris (`input()`) + `--yes` menerima default. Ini juga jalur tes.
- **Single-window list + detail** sudah cukup (keputusan #3).

### 5.2 Layar
1. **Input** — URL, lalu nama (menampilkan default dari `derive_name`).
2. **Progres** — teks status clone.
3. **Review draft** — daftar komponen; per baris: nama, type, source (`auto`/`guess`/`ask`). Aksi: `Enter` detail/edit, `a` tambah, `d` hapus, `t` ubah type, `e` edit field, `u` undo, `s` simpan, `q` batal. Field `guess`/`ask` ditonjolkan di atas dan wajib dilihat sebelum simpan.
4. **Gap** — daftar temuan (§6) dengan tombol approve/skip per item.
5. **Preview plan** — jalankan `compute_plan` + `validate` + dry-run, tampilkan `to_stop/to_start/to_swap` (mirip output `switch --dry-run`).
6. **Konfirmasi switch** — opsional; default **tidak** switch otomatis (user menjalankan `switch` sendiri), kecuali `--switch`.
7. **Cleanup** — prompt hapus `.bak-*` (§7).

### 5.3 Prinsip UI
- Tidak ada aksi destruktif tanpa konfirmasi eksplisit.
- Pindah `q` membatalkan tanpa menyimpan.
- Semua keputusan TUI dapat direpresentasikan sebagai flag agar bisa diuji tanpa curses.

### 5.4 Acceptance (Fase 3)
- ✅ Uji logika TUI tanpa curses (driver headless): urutan keputusan → draft final yang diharapkan (`tests/test_slice3a_review.py`, `tests/test_slice3a_review_cli.py`).
- ✅ Edit ditolak dengan benar (executable sesi terlarang, `argv_match` kosong tanpa `exact_argv`, `stop` compositor terkunci `"true"`, `pkill -f`) dan tidak mengubah draft.
- ✅ `q`/EOF membatalkan **tanpa** menyimpan; `--yes` berhenti (exit ≠ 0) bila masih ada field `ask`.
- ✅ Simpan lewat `tomlwrite` → validate; error validate membatalkan tulis (`tests/test_slice3a_generate_review.py`).
- ⬜ Uji smoke `curses` di pseudo-TTY (`pty`) minimal — Fase 3b.

### 5.5 Implementasi Fase 3a (revisi #4)
- **`rice_switcher/review.py`** — model murni (tanpa I/O). `ReviewSession` membungkus `ManifestDraft`; operasi: `set_type`, `set_field` (nama/start/executable/argv_match/exact_argv/stop/start_check), `add`, `remove`, `move`, `undo` (satu langkah, dapat ditumpuk). Setiap edit divalidasi memakai aturan yang sudah ada; field yang diedit user menjadi `auto`; `stop` compositor terkunci `"true"`. `findings()` meneruskan `ManifestDraft.findings` (kosong di 3a; Fase 4 mengisi).
- **`rice_switcher/review_cli.py`** — driver prompt baris-per-baris. `ask`/`out` disuntikkan (tak pernah memanggil `input()` langsung), sehingga hermetis. Ringkasan menonjolkan field `guess`/`ask` sebelum daftar komponen; perintah `Enter/e/a/d/t/u/s/q`; input tak dikenal dan edit yang ditolak hanya mencetak pesan lalu bertanya lagi; EOF = batal.
- **`cli.generate`** — flag `--no-tui` (prompt baris) dan `--yes` (terima default tanpa mengubah apa pun). `--yes` menolak simpan bila ada field `ask` (exit 3, daftar ditulis ke stderr). Jalur simpan menserialisasi dengan `tomlwrite`, mem-parse ulang, memvalidasi, dan **tidak menulis** bila ada error; `[meta].name` dipaksa sama dengan nama folder rice. `generate` tanpa flag tetap mencetak draft (perilaku lama tak berubah).

---

## 6. Fase 4 — Deteksi gap + approval (pola auto, eksekusi manual)

> **Cakupan revisi #5 = "Fase 4 sempit" (implemented).** Yang **selesai**: deteksi baca-saja + pelaporan `autostart_conflict` dan `needs_materialization`, field `started_by_config`, penanganan di review (`c`) dan `generate --yes` (terapkan default, berhenti pada yang tak tertangani). Yang **DI LUAR cakupan** (belum): eksekusi hook/skrip, approve skrip, `[hooks].pre` otomatis, deteksi `install.sh`/README, deteksi binary hilang (sudah di `validate`), `curses` (3b), cleanup (5), monitor (6), façade `onboard`.

### 6.0 Keputusan Langkah 0 (tercatat) + implementasi
Analisis kode (bukan asumsi) menentukan tiga hal; keputusan user mengikutinya:

1. **`start="true"` bukan no-op aman bagi `switch`.** `compute_plan` mengabaikan `start` (`managed_set.py:202-211`): identitas yang tidak aktif masuk `to_start` apa pun nilai `start`. `start_component` hanya memakai `start="true"` untuk **membungkam guard gagal-spawn** (`lifecycle.py:200-203`); ia **tetap** menunggu `start_check` via `wait_for` (`lifecycle.py:204-207`) dan melempar `LifecycleError` bila tak pernah lolos → switch gagal + rollback. Jadi `start="true"` **tidak** cukup untuk "dijalankan oleh config".
2. **Opsi B hanya benar untuk `exec`.** `exec` jalan ulang tiap `hyprctl reload` (reload terjadi di switch, `switch.py:307-313`), jadi config menghidupkannya kembali → switcher cukup stop. `exec-once`/`hl.on("hyprland.start")` hanya sekali saat login → kalau switcher stop, **tak ada yang menghidupkan** sampai login lagi → Opsi B **rusak** untuk keduanya.
3. **Butuh field eksplisit.** "no-op start" tak cukup (identitas tetap masuk `to_start`; `start` kosong = error validasi `validate.py:296-297`). Dipilih field eksplisit **`started_by_config: bool = False`** (pola `exact_argv`) + `compute_plan` mengeluarkannya dari `to_start`. **Default per jenis:** `exec` → Opsi B (`config`); `exec-once`/`hl.exec_cmd` → **tanpa default** (`ask`; `--yes` berhenti). Opsi C (switcher start+stop, peringatan "start ganda sekali saat login") boleh dipilih manual. **Opsi A** (netralkan baris config) **belum** diimplementasikan — hanya dilaporkan sebagai opsi tak tersedia.

Implementasi: `rice_switcher/gaps.py` (fungsi murni `autostart_conflict`/`autostart_conflicts`/`materialisation_signals`/`needs_materialization`/`collect_findings`; **baca-saja**, tak mengeksekusi apa pun), `detect()` mengisi `ManifestDraft.findings` dari `autostart_refs_by_name` (hanya ref `reachable`), `compute_plan` menambah `SwitchPlan.to_config`, `switch._print_plan` menampilkan "dijalankan oleh config", `validate` menolak `started_by_config` pada compositor. Review: `ReviewSession.handle_finding`/`apply_default_findings`/`pending_findings` (dicatat di riwayat, bisa di-`undo`), perintah **`c`** di `review_cli.py`, dan `generate --yes` berhenti pada temuan tak tertangani (`cli.py`).

### 6.1 Pola yang **bisa** dikenali otomatis
| Pola | Arti | Saran |
|---|---|---|
| **Autostart bentrok** (`autostart_conflict`): config menjalankan komponen (`exec-once`/`exec`/`hl.exec_cmd`) yang juga dijadikan komponen terkelola (§4.1) — hanya dari berkas **reachable** | start ganda / saling bunuh saat reload | Dua opsi: **Opsi B** (`config`) = tandai `started_by_config = true` → switcher **hanya stop** (default untuk `exec`); **Opsi C** (`switcher`) = switcher start+stop (peringatan "start ganda sekali saat login"). **Tanpa default** (`ask`) untuk `exec-once`/`hl.exec_cmd`. **Opsi A** (netralkan baris) belum ada — hanya dilaporkan, jangan untuk Lua (rapuh). |
| **Butuh materialisasi** (`needs_materialization`): tak ada entrypoint Hyprland yang terbaca, tapi ada `profiles/`, `settings.lua`, `variables.lua`, atau `*.sh` dengan subcommand `apply` | rice butuh **materialisasi** (mis. elifouts: `hypr-profile.sh apply` menghasilkan `hyprland.lua` root) | laporkan path + isi sinyal; **skrip tidak dijalankan** (butuh approve eksplisit, fase lain). `--yes` berhenti. |
| Config `@import`/ref ke file yang tak ada & diproduksi tool dikenal (mis. `colors-waybar.css` dari pywal/wal/matugen) | dependency generasi warna | sarankan install/gen atau `[hooks].pre` |
| Ada `install.sh`/`setup.sh`/`Makefile`/`justfile`/`bootstrap` | kandidat prasyarat | tandai (jangan jalankan) |
| README menyebut "run ./install.sh first" | petunjuk (akurasi rendah) | peringatan |
| Symlink keluar folder rice | berbahaya | tolak + peringatan (§10) |
| Binary hilang (`validate`) | dep sistem | sarankan `pacman -S …` |

### 6.2 Eksekusi (approve eksplisit + tampilkan isi)
> **Revisi #5: eksekusi BELUM ada.** Deteksi + penanganan saja; **tak ada** skrip/hook pihak ketiga yang dijalankan dari repo rice. Bagian ini untuk fase eksekusi berikutnya.
- Setiap item gap → **prompt approve/skip**, default skip. Tidak pernah silent.
- **Sebelum approve, tampilkan isi skripnya** (atau minimal path + opsi buka di pager). `shlex.split` mencegah injeksi shell, **tapi tidak** membuat `hypr-profile.sh apply` aman — itu tetap kode pihak ketiga.
- Bila disetujui, langkah dijalankan via `runner` (argv, tanpa shell) dan dicatat di audit.
- Kalau langkah dimasukkan ke `[hooks].pre`, ia **jalan di setiap switch** → gerbang konfirmasi hash rice (yang sudah ada di `switch`) harus **secara eksplisit mencakup** perubahan `hooks`.
- Tidak bisa diotomatiskan: menjalankan kode pihak ketiga secara aman buta, mengadakan data eksternal (wallpaper+`wal`), memilih varian profil.

### 6.3 Acceptance (Fase 4)
**Terpenuhi di revisi #5 (Fase 4 sempit):**
- Dua jenis temuan terstruktur: `autostart_conflict` dan `needs_materialization`; masing-masing muncul untuk fixture yang benar, **tidak** muncul untuk rice bersih, dan **tidak** dipicu berkas tak terjangkau (`archive`/`backup`/`example`, symlink keluar) — `tests/test_slice4_gaps.py`, `tests/test_slice4_detect_findings.py`.
- Deteksi **autostart bentrok**: fixture `exec = waybar` **dan** komponen waybar terkelola → temuan dengan dua opsi + default `config`; `exec-once`/`hl.exec_cmd` → tanpa default. `reachable=False` tidak memicu.
- **`started_by_config`**: manifest lama parse identik (default `False`), `Identity`/posisional lama tak berubah, `compute_plan` mengeluarkannya dari `to_start` tapi tetap boleh `to_stop`, dry-run menampilkan "dijalankan oleh config", `validate` menolaknya untuk compositor — `tests/test_slice4a_started_by_config.py`.
- Review menampilkan temuan di atas daftar komponen; perintah **`c`** memilih opsi default / opsi lain / skip, **tercatat di riwayat** dan bisa di-`undo` — `tests/test_slice4_review_findings.py`.
- `generate --yes` menerapkan opsi default lalu **berhenti tanpa menulis** (exit non-zero + daftar) pada `autostart_conflict` tanpa default dan pada `needs_materialization`.
- **Tak ada eksekusi** dari repo rice: pemindai materialisasi hanya membaca; tes menjaga seam proses (`subprocess.run/Popen`, `os.system/os.popen/os.execv/os.spawnv`) menggagalkan bila tersentuh — `tests/test_slice4_detect_findings.py::test_apply_script_is_never_executed`.
- e2e hermetik: `clone` → `generate --no-tui` (jawaban skrip menangani bentrok) → `validate` → `switch --dry-run`; komponen ber-opsi-default **tidak** masuk `to_start` (bukti tak ada start ganda) dan tak ada tulisan di luar tempdir — `tests/test_slice4_e2e.py`.

**Ditunda ke fase eksekusi (belum):** menjalankan kode pihak ketiga secara aman buta, approve + eksekusi skrip materialisasi (`[hooks].pre`), menampilkan isi skrip sebelum approve di jalur eksekusi, mengadakan data eksternal, memilih varian profil. Opsi A (netralkan baris config) juga belum diimplementasikan.

---

## 7. Fase 5 — Hapus backup (BISA) — ✅ standalone (revisi #6); prompt di `switch`/`onboard` belum

> **Cakupan revisi #6 = versi standalone.** Yang **selesai**: subcommand `rice-switcher clean-backups [--keep N] [--yes]` (`rice_switcher/cli.py::cmd_clean_backups`) + logika murni `rice_switcher/backups.py`. Yang **belum**: prompt cleanup otomatis di dalam `switch`/`onboard` (menunggu façade `onboard` di Fase akhir) dan penawaran retensi pasca-switch.

- Sumber `.bak-<ts>`: dibuat `switch` saat swap (`snapshot.py::swap_symlink`) sebagai `<target>.bak-<ts>` **di dalam `roots.config_home`** (sebelah `config_link`), dengan `ts = _now_ts()` = `%Y%m%dT%H%M%S_<3-digit ms>`. Entri bisa berupa **symlink** (kasus umum, `config_link` menunjuk pohon rice) atau **direktori nyata** (swap pertama saat path live memang direktori nyata).
- **Pola persis.** `list_backups(config_home)` hanya menerima nama `<name>.bak-<8digits>T<6digits>_<3digits>`; `.bak-` tanpa timestamp, `foo.bak-1`, dan varian `keybind-install` (`%Y%m%d-%H%M%S`, `keybind.py::_backup_existing`) **tidak** cocok dan tidak disentuh. Urutan berdasarkan timestamp **di nama** (leksikografis == kronologis), **bukan** mtime. `lstat`, tidak pernah mengikuti symlink.
- **`plan_cleanup(entries, keep)`** murni/tanpa efek samping: `keep=N>0` menyisakan N terbaru; `keep=0`/`None` = semua kandidat. `remove_backup(path, config_home)`: containment `os.path.commonpath` atas `realpath(config_home)` vs `realpath` **induk** path (entri terakhir sengaja tidak di-resolve agar symlink tidak diikuti), di luar → ditolak.
- **Hapus aman (no-follow).** Symlink → `os.unlink` (hapus link saja, **target tetap utuh**, termasuk bila target di luar `config_home`); direktori **nyata** (`lstat`→`S_ISDIR`, bukan symlink) → `shutil.rmtree`; jenis lain → ditolak. Tidak pernah memutuskan lewat `os.path.isdir`.
- **Penjaga switch berjalan.** `cmd_clean_backups` menolak berjalan (exit non-nol, tidak menghapus apa pun) bila `switch_lock` (`lock.py`) sedang dipegang — yaitu saat `switch`/`rollback` berlangsung. Lock di-`probe` sebelum prompt dan diambil kembali saat penghapusan.
- **Konsekuensi disebut di output:** `undo_swap` memakai `.bak-*` sebagai jalur rollback cepat; snapshot (`<state_home>/rice-switcher/snapshot/<ts>/`) adalah fallback. Menghapus `.bak-*` menurunkan jalur cepat → tinggal snapshot.
- **Prompt:** tanpa `--yes` → tampilkan daftar yang akan dihapus + yang disimpan, lalu `hapus N backup? [y/N]` dengan **default N**; EOF/input kosong = batal (exit 0, tidak menghapus). `--yes` melewati prompt tapi tetap menampilkan daftar + konsekuensi. `--keep` divalidasi integer `>= 0` (salah → exit non-nol, tidak menghapus). Tidak ada kandidat → pesan jelas, exit 0. Kegagalan hapus satu entri dilaporkan tanpa menghentikan yang lain → exit non-nol.
- Audit `{"action":"cleanup","removed":[…],"failed":[{"path":…,"error":…}]}`.

### 7.1 Acceptance (Fase 5)
**Terpenuhi (standalone, revisi #6):**
- Hapus hanya pola yang benar; `--keep 1` menyisakan yang terbaru; `--yes` non-interaktif; path di luar `config_home` ditolak — `tests/test_slice5_backups.py`, `tests/test_slice5_clean_backups.py`.
- Symlink dihapus dengan `unlink` (target tetap utuh, termasuk target di luar `config_home`); direktori nyata berisi symlink keluar → isi luar tetap utuh.
- Switch berlangsung (`switch_lock` dipegang) → ditolak; batal/EOF → tidak menghapus; audit tertulis; e2e hermetis `switch` ×2 → `clean-backups --keep 1 --yes` → state/rice aktif tak berubah, snapshot fallback tetap ada — `tests/test_slice5_e2e.py`.

**Belum:** prompt cleanup otomatis di dalam `switch`/`onboard` (menunggu façade).

---

## 8. Fase 6 — Deteksi/patch monitor (BISA)

### 8.1 Deteksi
- `hyprctl monitors -j` → JSON: `name`, `width`, `height`, `refreshRate`, `focused`, dan mode tersedia.
- Pilih **primary** = monitor `focused` (fallback: pertama).
- Injectable `hyprctl` runner (pola yang sama) agar hermetis.

### 8.2 Patch
- Cari baris `monitor=` di `hyprland.conf`/`*.conf` dan panggilan `hl.monitor(...)` di `*.lua`.
- **Aturan aman:** JANGAN sentuh `monitor=,preferred,…`/`auto` (memang portabel). Hanya normalisasi baris eksplisit yang menyebut monitor nyata.
- Hasil patch = `monitor = <name>, <W>x<H>@<R>, <x>x<y>, <scale>`.
- Tampilkan **diff** di TUI untuk konfirmasi; `--monitor "<name,WxH@R>"` untuk override manual.
- Multi-monitor: daftar semua; user pilih/petakan; default = primary saja.

### 8.3 Acceptance (Fase 6)
- Input JSON `hyprctl` palsu (single & multi monitor, ada `preferred`) → patch benar; baris `preferred` tak tersentuh; diff ditampilkan.

### 8.4 Patch sebagai commit lokal (tambahan)
- Simpan hasil patch monitor + normalisasi layout sebagai **commit lokal di branch terpisah** (mis. `rice-switcher/local`) agar mudah `git diff`/`revert`/`rebase` bila upstream berubah. Jangan commit ke branch default repo clone.

---

## 9. Provenance & state

- **Provenance di file terpisah** `<rice>/.rice-source.toml` (keputusan #4), **bukan** di `[meta]`: URL bisa berisi `https://user:token@…` dan `rice.toml` mungkin kamu commit/bagikan. Isi: `source_url` (URL **di-redact**), `cloned_at`, `cloned_rev`. File ini **di-exclude** dari git via `<rice>/.git/info/exclude`.
- **Tidak** menambah field `Meta` (`manifest.py:68-74`).
- State: **tidak** ada field baru yang wajib. `adopt` (sudah ada) bisa dipakai setelah onboarding bila user ingin menjadikan sesi baru sebagai "current".
- Tidak ada format snapshot/audit baru; memakai yang sudah ada.

---

## 10. Keamanan

- **Argument injection (clone).** URL diawali `-` ditolak; argv memakai `git clone -- <url>`; `GIT_TERMINAL_PROMPT=0` + `GIT_ASKPASS=echo`; protokol dibatasi, `ext::` diblokir (§3.2).
- **Nama rice.** `.`/`..` sudah ditolak (`paths.py:40-41`); tambahan: **tidak boleh berawalan titik** (bentrok temp dir `.<name>.tmp-<pid>`; regex `paths.py:28` meloloskannya).
- **Containment symlink.** Tidak mengikuti symlink yang `realpath`-nya keluar folder rice — saat scan (`detect.py`), normalisasi layout, dan cleanup. Bandingkan `os.path.realpath(target)` dengan `os.path.realpath(rice_dir)` via `os.path.commonpath`; tolak bila di luar.
- **Cleanup.** `.bak-*` dihapus dengan `lstat` + `unlink` (symlink) / `rmtree` (dir nyata non-symlink); tidak pernah mengikuti target; nama harus cocok **persis** pola `_now_ts()`; containment `commonpath` pada induk yang di-`realpath`; menolak berjalan selama `switch_lock` dipegang (§7).
- **Eksekusi prasyarat.** Hanya dengan approve eksplisit **setelah menampilkan isi skrip**; `runner` argv tanpa shell; audit. `[hooks].pre` yang ditambahkan otomatis ikut gerbang konfirmasi hash rice.
- **Lintasan path** (`config_link`, lokasi swap) diverifikasi di bawah `roots` seperti mekanisme yang ada (`docs/PLAN.md §2h`, `validate.py`).
- **Redaksi URL** saat logging **dan** penyimpanan (hindari token) — audit & `.rice-source.toml`.
- Tidak ada `eval`/`shell=True` atas isi repo.

---

## 11. Rencana pengujian

- **Hermetis**: semua `subprocess` (`git`, `hyprctl`) distub via PATH / runner injectable. Tidak menyentuh `~/.config`/`~/.local`, tidak kill proses nyata.
- **Fixture**: pakai yang sudah ada di `fixtures/` (`hyprland`, `waybar`, `quickshell`, `illogical-impulse`, `matugen`, dst.) sebagai kasus deteksi; tambah fixture: "profil tanpa config root", repo dengan `exec-once` **dan** komponen terkelola (autostart bentrok), dan repo berisi symlink keluar.
- **Round-trip** `tomlwrite` vs `tomllib`.
- **Keamanan (regresi)**: URL diawali `-` ditolak; `ext::` ditolak; `git` dipanggil dengan `--`; `GIT_TERMINAL_PROMPT=0`; nama berawalan titik ditolak; symlink keluar folder rice ditolak (detect + cleanup); `.bak-*` symlink dihapus dengan `unlink` (target tetap utuh); nama non-pola (`foo.bak-1`, varian keybind) tak tersentuh; path di luar `config_home` ditolak; cleanup menolak saat `switch_lock` dipegang; tak ada eksekusi skrip tanpa approve.
- **Prasyarat 0**: `stop` kosong → kill-by-identity benar-benar menghentikan proses uji (`sleep`, stubbed); rice yang punya `stop` tidak berubah perilakunya.
- **Design tests** (`tests/design/`) untuk properti heuristik & batas aman.
- **Non-TUI**: setiap fase dites lewat flag-nya (tanpa curses).
- Suite penuh harus tetap hijau; tambahan tes per fase.

---

## 12. Urutan rollout & acceptance

**Prasyarat:** core `switch` harus sudah pernah dijalankan sungguhan di desktop asli (minimal dry-run) sebelum onboarding — onboarding bergantung pada konvensi swap-nya. *(Sudah terpenuhi: switch + rollback nyata pada rice `elifouts`/baseline `end4`.)*

| Fase | Isi | Bergantung | Definition of done |
|---|---|---|---|
| **P0** | **Prasyarat 0**: kill-by-identity (`process.py`/`lifecycle.py`) + tolak nama berawalan titik | — | `stop` kosong → komponen benar-benar berhenti; rice yang punya `stop` tak berubah; tes hijau |
| 1 | `clone` + naming (hardened) | P0 | BISA penuh; tes keamanan hijau |
| 2 | `detect` + `tomlwrite` (draft) | 1 | draft benar untuk fixture; round-trip hijau |
| 3a | review/edit via **prompt baris-per-baris** ✅ | 2 | draft bisa di-review & disimpan tanpa curses |
| 3b | TUI `curses` (pembungkus) | 3a | single-window list+detail; jalur non-TTY tetap ada |
| 4 | deteksi gap + penanganan (+ autostart bentrok) | 3a | gap terdeteksi; `--yes` berhenti pada temuan tak tertangani; **tak ada eksekusi** dari repo rice ✅ revisi #5 |
| 5 | prompt cleanup | 3a | hapus aman (no-follow) + retensi + konsekuensi disebut ✅ revisi #6 (**standalone**; prompt di `switch`/`onboard` menunggu façade) |
| 6 | monitor deteksi/patch (commit branch) | 3a | patch aman (preferred tak tersentuh) |
| — | `onboard` orkestrator | 1-6 | end-to-end semi-otomatis |

Tiap fase: commit terpisah, suite hijau, **tanpa** mengubah live desktop.

---

## 13. Non-goals (batas eksplisit)

- Menjamin `rice.toml` **final & selalu benar** tanpa review user. → tidak bisa; karena itu review-first.
- Menjalankan/mengotomatiskan prasyarat pihak ketiga secara aman tanpa approve.
- Mengadakan data eksternal (wallpaper, `wal`, paket sistem).
- Menambah dependency runtime (tidak pakai `textual`/`rich`).
- Menambah field `state`/snapshot/audit baru yang wajib.

---

## 14. Keputusan (dari review, revisi #2)

1. **Layout** (§4.3): symlink relatif, **didaftarkan** di `<rice>/.git/info/exclude` agar repo clone bersih & `git pull` tak bentrok.
2. **`stop` default** (§4.2): **dikosongkan**; switcher memakai stop bawaan berbasis PID (identitas `executable` + `argv_match`, `docs/PLAN.md §2g`). Jangan menebak `pkill -x`; tandai `ask` bila ragu. Butuh Prasyarat 0.
3. **TUI**: single-window list + detail; **dibuat paling akhir** (fallback prompt dulu).
4. **Provenance**: file terpisah `.rice-source.toml` (di-exclude), URL di-redact.
5. **`--switch`**: tawarkan **setelah** validate hijau **dan** plan dry-run ditampilkan; default **"N"**.
6. **Subcommand**: keduanya — `onboard` sebagai façade, `clone`/`generate` sebagai pecahan.
7. **Nama**: tidak dipaksa lowercase, tapi **tolak** `.`/`..` dan nama **berawalan titik**.

### 14.1 Masih terbuka (non-blocking)
- Nama branch lokal untuk patch monitor/layout (usul: `rice-switcher/local`).
- Retensi default `.bak-*` (selalu tanya, atau simpan N terbaru).
