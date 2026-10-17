# Design — Onboarding TUI: clone, auto-manifest, gap detection

**Status:** draft **revisi #2** — Prasyarat 0 (kill-by-identity) dan **Fase 1 (clone + naming)** sudah diimplementasikan & diuji; Fase 2–6 belum.
**Status implementasi:** ✅ Prasyarat 0 (§12) — `process.py`/`lifecycle.py`/`managed_set.py`; ✅ Fase 1 clone + naming (§3, §9, §10) — `rice_switcher/clone.py`, subcommand `clone`, `tests/test_clone.py`; ⬜ Fase 2–6.
**Cakupan:** alur "clone rice orang lain → bisa di-`switch`" menjadi semi-otomatis lewat TUI.
**Dokumen terkait:** `docs/PLAN.md` (spec inti switch; §2c managed set, §2f `config_link`, §2g identitas proses, §2h keamanan, §4.1 skema manifest, §5.1 zero-dep, §6 roadmap), `README.md`.
**Sifat:** dokumen desain. Tidak mengubah `docs/PLAN.md`; mekanisme inti switch tetap apa adanya — **kecuali** Prasyarat 0 (kill-by-identity, §12) yang menyentuh `process.py`/`lifecycle.py`.

**Riwayat revisi**
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

Contoh nyata (elifouts): `execs.lua:6 → hl.exec_cmd("qs -c $qsConfig")` + `variables.lua:5 → hl.env("qsConfig","ii")` ⇒ komponen quickshell `qs -c ii`. Parser harus mengikuti indireksi variabel ini.

**⚠ Autostart bentrok (temuan baru — wajib, lihat §6.1).** Baris autostart itu **tetap ada** di config rice. Kalau detector menjadikannya komponen terkelola, `switch` akan **start** komponen itu, sementara `exec-once`/`exec` di config juga menjalankannya → **start ganda**, atau **saling bunuh** saat reload (`exec` jalan ulang tiap `hyprctl reload`; `exec-once` hanya sekali). Karena itu detector **wajib** memunculkan temuan "autostart bentrok" dengan dua opsi penanganan (§6.1) — **default Opsi B** ("dijalankan oleh config": switcher hanya stop) — **tidak** diam-diam membuat komponen start+stop.

### 4.2 Yang butuh keputusan / perhatian user
- **`stop` → dikosongkan (keputusan #2).** Generator **tidak mengisi `stop`** dan **tidak menebak `pkill -x`**. `stop` kosong → `stop_component` memakai stop bawaan berbasis PID (identitas `executable` + `argv_match`, `docs/PLAN.md §2g`). Ini bergantung pada **Prasyarat 0** (§12): kill-by-identity di `process.py`/`lifecycle.py`; tanpa itu `stop` kosong = jalankan `true` lalu **menunggu** → timeout (`lifecycle.py:119-136`). Kalau identitas ambigu → tandai `ask`, jangan mengarang `stop`.
- `argv_match` bila ambigu (variabel tak teresolusi, quoting, beberapa instance).
- `type` bila ambigu ("shell" vs "bar").
- `name` komponen bila bentrok.
- **Urutan** start antar komponen.

Draft menandai tiap field dengan tingkat keyakinan (`auto` / `guess` / `ask`). TUI menonjolkan yang `guess`/`ask`.

**Guard kill-by-identity (brief Prasyarat 0).** Ini kode baru yang bisa **membunuh proses**, dan `executable`/`argv_match` bisa berasal dari `rice.toml` orang lain. Wajib:
- Hanya PID milik **uid sendiri**; abaikan **PID 1**, PID switcher sendiri, dan **parent**-nya.
- Cocokkan **argv lengkap** (identitas `executable` + `argv_match`), **bukan** basename saja — kalau tidak, `qs -c ii` dan `qs -c minimal` dianggap sama. Tolak/lewati bila `argv_match` kosong (terlalu luas).
- **Baca ulang `/proc/<pid>/cmdline` tepat sebelum kill** (cegah PID reuse).
- **SIGTERM → tunggu (timeout) → SIGKILL**.
- **Lindungi proses sesi**: manifest dengan `executable` = `Hyprland`/`systemd`/`dbus-daemon`/`gnome-keyring-daemon` (dan sejenisnya) **ditolak validator** — ada tesnya.
- **Gerbang konfirmasi menampilkan target**: saat ini konfirmasi `yes` untuk rice baru menampilkan command string, padahal kill-by-identity bukan string → tambahkan baris **"akan menghentikan proses: …"**.

### 4.3 Layout → `config_link` (keputusan #1)
Konvensi swap: target = `<RICE_HOME>/<name>/<basename(config_link)>` (`switch.py:268-271`). Artinya config harus ada di **root rice** dengan nama `basename`-nya.

Dua bentuk repo:
- **(a) Gaya `~/.config`**: repo punya `.config/hypr`, `.config/waybar`, … → generator memetakan `config_link="~/.config/hypr"`, tetapi **harus menormalkan layout** ke root rice.
- **(b) Gaya polos**: repo punya `hypr/`, `waybar/`, … langsung di root → langsung cocok.

**Keputusan:** normalisasi ke root rice dengan **symlink relatif** (`<name>/hypr -> .config/hypr`) supaya tidak menggandakan file besar. Symlink yang dibuat generator **didaftarkan di `<rice>/.git/info/exclude`** agar repo clone tetap bersih dan `git pull` tidak bentrok.

**Containment wajib:** saat scan (`detect.py`) maupun saat membuat/menghapus symlink normalisasi, **jangan mengikuti** symlink yang `realpath`-nya keluar dari folder rice. Repo asing bisa berisi `hypr -> /etc` atau `waybar -> ~/.ssh`; tanpa aturan ini, isi pribadimu bisa terbaca atau ikut ter-swap. Detail: §10.

### 4.4 Anomali yang dideteksi (bukan gap, tapi diperingatkan)
- Tak ada config yang dikenal → "rice ini mungkin bukan rice Hyprland".
- `config_link` bentrok dengan rice lain (dicek `validate`).
- **Symlink keluar folder rice** (`realpath` di luar) → ditolak/diabaikan + peringatan (§10).
- Binary hilang (mis. `pypr`, `wal`) → warning (bukan error), konsisten `validate`.

### 4.5 `tomlwrite.py`
- `tomllib` hanya baca → serializer minimal menulis: string, int, bool, array of string, tabel, array-of-tables.
- Urutan key mengikuti `docs/PLAN.md §4.1`: **scalar → tabel → array-of-tables**.
- Tes: round-trip `tomllib.loads(tomlwrite(draft)) == draft` untuk ~15 manifest contoh (termasuk `fixtures/**`).

### 4.6 Acceptance (Fase 2)
- `detect` pada fixture `fixtures/hyprland`, `fixtures/waybar`, `fixtures/quickshell`, `fixtures/illogical-impulse` menghasilkan komponen yang diharapkan.
- Field `stop`/`argv_match` ambigu ditandai `ask` (bukan dipaksakan).
- Round-trip TOML hijau.

---

## 5. Fase 3 — TUI review/edit (BISA)

### 5.1 Teknologi & urutan pembangunan
- **Prompt baris-per-baris dibangun LEBIH DULU; `curses` paling akhir** (urutan review #1). Jalur non-interaktif memang wajib (`--no-tui`/non-TTY), jadi TUI hanya pembungkus tipis di atasnya. Dengan ini Fase 1–2 (clone + detect + tomlwrite) sudah berguna tanpa menunggu TUI.
- **`curses` (stdlib)** untuk TUI. Alasan: zero-dep (`docs/PLAN.md §5.1`). Bila `stdout` bukan TTY atau `--no-tui` → pakai prompt baris-per-baris (`input()`) + `--yes` menerima default. Ini juga jalur tes.
- **Single-window list + detail** sudah cukup (keputusan #3).

### 5.2 Layar
1. **Input** — URL, lalu nama (menampilkan default dari `derive_name`).
2. **Progres** — teks status clone.
3. **Review draft** — daftar komponen; per baris: nama, type, source (`auto`/`guess`/`ask`). Aksi: `Enter` detail/edit, `a` tambah, `d` hapus, `t` ubah type, `e` edit field, `s` simpan.
4. **Gap** — daftar temuan (§6) dengan tombol approve/skip per item.
5. **Preview plan** — jalankan `compute_plan` + `validate` + dry-run, tampilkan `to_stop/to_start/to_swap` (mirip output `switch --dry-run`).
6. **Konfirmasi switch** — opsional; default **tidak** switch otomatis (user menjalankan `switch` sendiri), kecuali `--switch`.
7. **Cleanup** — prompt hapus `.bak-*` (§7).

### 5.3 Prinsip UI
- Tidak ada aksi destruktif tanpa konfirmasi eksplisit.
- Pindah `q` membatalkan tanpa menyimpan.
- Semua keputusan TUI dapat direpresentasikan sebagai flag agar bisa diuji tanpa curses.

### 5.4 Acceptance (Fase 3)
- Uji logika TUI tanpa curses (driver headless): urutan keputusan → draft final yang diharapkan.
- Uji smoke `curses` di pseudo-TTY (`pty`) minimal (opsional).

---

## 6. Fase 4 — Deteksi gap + approval (pola auto, eksekusi manual)

### 6.1 Pola yang **bisa** dikenali otomatis
| Pola | Arti | Saran |
|---|---|---|
| **Autostart bentrok**: config menjalankan komponen (`exec-once`/`exec`/`hl.exec_cmd`) yang juga dijadikan komponen terkelola (§4.1) | start ganda / saling bunuh saat reload | **Default: Opsi B** — tandai komponen "dijalankan oleh config" → switcher **hanya stop**, tidak start. **Opsi A** (netralkan baris) hanya untuk baris `exec-once` sederhana; **jangan** untuk Lua (`hl.exec_cmd` di dalam fungsi / pakai variabel — rapuh). |
| Tak ada config root, tapi ada `profiles/`, `settings.lua`, `variables.lua`, `*.sh` dengan subcommand `apply` | rice butuh **materialisasi** (mis. elifouts: `hypr-profile.sh apply` menghasilkan `hyprland.lua` root) | tambahkan `[hooks].pre` (dengan persetujuan) |
| Config `@import`/ref ke file yang tak ada & diproduksi tool dikenal (mis. `colors-waybar.css` dari pywal/wal/matugen) | dependency generasi warna | sarankan install/gen atau `[hooks].pre` |
| Ada `install.sh`/`setup.sh`/`Makefile`/`justfile`/`bootstrap` | kandidat prasyarat | tandai (jangan jalankan) |
| README menyebut "run ./install.sh first" | petunjuk (akurasi rendah) | peringatan |
| Symlink keluar folder rice | berbahaya | tolak + peringatan (§10) |
| Binary hilang (`validate`) | dep sistem | sarankan `pacman -S …` |

### 6.2 Eksekusi (approve eksplisit + tampilkan isi)
- Setiap item gap → **prompt approve/skip**, default skip. Tidak pernah silent.
- **Sebelum approve, tampilkan isi skripnya** (atau minimal path + opsi buka di pager). `shlex.split` mencegah injeksi shell, **tapi tidak** membuat `hypr-profile.sh apply` aman — itu tetap kode pihak ketiga.
- Bila disetujui, langkah dijalankan via `runner` (argv, tanpa shell) dan dicatat di audit.
- Kalau langkah dimasukkan ke `[hooks].pre`, ia **jalan di setiap switch** → gerbang konfirmasi hash rice (yang sudah ada di `switch`) harus **secara eksplisit mencakup** perubahan `hooks`.
- Tidak bisa diotomatiskan: menjalankan kode pihak ketiga secara aman buta, mengadakan data eksternal (wallpaper+`wal`), memilih varian profil.

### 6.3 Acceptance (Fase 4)
- `fixtures/illogical-impulse` dan struktur "profil tanpa config root" memicu temuan "butuh materialisasi".
- Deteksi **autostart bentrok**: fixture dengan `exec-once = waybar` **dan** komponen waybar terkelola → memunculkan temuan + dua opsi.
- Tak ada eksekusi tanpa approve (dites dengan runner palsu yang mencatat panggilan); isi skrip tampil sebelum approve.

---

## 7. Fase 5 — Prompt hapus backup (BISA)

- Sumber `.bak-<ts>`: dibuat `switch` saat swap (lihat riwayat).
- Setelah switch sukses, `onboard`/`switch` menawarkan: "Hapus backup? [y/N]" + opsi retensi `--keep N` (default: tanya; simpan N terbaru bila diberi angka).
- **Hanya** menghapus yang cocok pola `.bak-<angka/timestamp>` milik tool; tidak pernah path sembarang. Verifikasi path berada di bawah `roots.config_home` (`os.path.commonpath`).
- **Hapus aman (no-follow).** `.bak-*` bisa berupa **symlink** ke folder rice. Untuk symlink → `os.lstat` + `os.unlink` (hapus link-nya saja, **jangan ikuti target**); `shutil.rmtree` hanya untuk direktori **nyata** (`lstat` → `S_ISDIR`, bukan symlink). Tidak pernah menghapus berdasarkan `os.path.isdir` yang mengikuti link.
- **Konsekuensi yang harus disebut di prompt:** rollback memakai `undo_swap` (`.bak-*`) sebagai jalur utama, snapshot sebagai fallback. Menghapus `.bak-*` menurunkan jalur cepat → tinggal snapshot.
- Bisa juga standalone: `rice-switcher clean-backups [--keep N] [--yes]`.
- Audit `{"action":"cleanup","removed":[...]}`.

### 7.1 Acceptance (Fase 5)
- Hapus hanya pola yang benar; `--keep 1` menyisakan yang terbaru; `--yes` non-interaktif; path di luar `config_home` ditolak.

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
- **Cleanup.** `.bak-*` dihapus dengan `lstat` + `unlink` (symlink) / `rmtree` (dir nyata non-symlink); tidak pernah mengikuti target (§7).
- **Eksekusi prasyarat.** Hanya dengan approve eksplisit **setelah menampilkan isi skrip**; `runner` argv tanpa shell; audit. `[hooks].pre` yang ditambahkan otomatis ikut gerbang konfirmasi hash rice.
- **Lintasan path** (`config_link`, lokasi swap) diverifikasi di bawah `roots` seperti mekanisme yang ada (`docs/PLAN.md §2h`, `validate.py`).
- **Redaksi URL** saat logging **dan** penyimpanan (hindari token) — audit & `.rice-source.toml`.
- Tidak ada `eval`/`shell=True` atas isi repo.

---

## 11. Rencana pengujian

- **Hermetis**: semua `subprocess` (`git`, `hyprctl`) distub via PATH / runner injectable. Tidak menyentuh `~/.config`/`~/.local`, tidak kill proses nyata.
- **Fixture**: pakai yang sudah ada di `fixtures/` (`hyprland`, `waybar`, `quickshell`, `illogical-impulse`, `matugen`, dst.) sebagai kasus deteksi; tambah fixture: "profil tanpa config root", repo dengan `exec-once` **dan** komponen terkelola (autostart bentrok), dan repo berisi symlink keluar.
- **Round-trip** `tomlwrite` vs `tomllib`.
- **Keamanan (regresi)**: URL diawali `-` ditolak; `ext::` ditolak; `git` dipanggil dengan `--`; `GIT_TERMINAL_PROMPT=0`; nama berawalan titik ditolak; symlink keluar folder rice ditolak (detect + cleanup); `.bak-*` symlink dihapus dengan `unlink` (target tetap utuh); tak ada eksekusi skrip tanpa approve.
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
| 3a | review/edit via **prompt baris-per-baris** | 2 | draft bisa di-review & disimpan tanpa curses |
| 3b | TUI `curses` (pembungkus) | 3a | single-window list+detail; jalur non-TTY tetap ada |
| 4 | deteksi gap + approve (+ autostart bentrok) | 3a | gap terdeteksi; tak ada eksekusi tanpa approve |
| 5 | prompt cleanup | 3a | hapus aman (no-follow) + retensi + konsekuensi disebut |
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
