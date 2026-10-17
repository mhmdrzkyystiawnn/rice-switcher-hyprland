Versi publik: docs/PLAN.md (seksi 2–7 + keputusan). File ini lokal, jangan di-track.

> **Spesifikasi publik.** Dokumen ini memuat spesifikasi inti (§2–§7) dan keputusan desain. Seksi §1 (inventaris mesin) dan §8 (temuan sensitif) sengaja tidak dipublikasikan; versi lengkap ada di `PLAN.local.md` (lokal).

## 1. Ringkasan Setup Saat Ini

> **Bagian ini sengaja tidak dipublikasikan** karena berisi data mesin penulis (inventaris sistem, monitor, proses, tema, dan path). Lihat `PLAN.local.md`.

## 2. Pemetaan Komponen → Field Manifest

### 2.0 Field Manifest (revisi #2)

**Per komponen** (`[[components]]` array):

| Field | Wajib | Arti |
|---|---|---|
| `name` | ya | identifier unik dalam satu rice. Dipakai oleh `depends_on` |
| `type` | ya | `bar` \| `shell` \| `notifications` \| `locker` \| `wallpaper` \| `compositor` \| `terminal` \| `launcher` \| `theme_generator` \| `color_daemon` \| `system` (geoclue, dbus, audio, dll.) \| `clipboard` |
| `executable` | tidak (default = `name`) | basename executable untuk process identity (§2g). Mis. Quickshell: `"qs"` |
| `argv_match` | tidak (default = `[]`) | list token yang HARUS ada di argv (subset, urutan tidak penting). Disambiguator `qs -c ii` vs `qs -c minimal` (§2g) |
| `exact_argv` | tidak (default = `false`) | bila `true`, argv proses harus **sama persis** dengan `executable` + `argv_match` (urutan penting, tanpa token tambahan). `exact_argv=true` + `argv_match=[]` menyatakan identitas **tanpa argumen** untuk daemon seperti waybar/hypridle (§2g) |
| `start` | ya* | command untuk start. Boleh pakai placeholder `{config_link}` |
| `stop` | ya* | command stop. Boleh `true` (noop) untuk komponen yang tidak di-manage (lihat §2c) |
| `stop_check` | tidak | cara verifikasi proses sudah mati (default: identity scan via `/proc`, lihat §2g) |
| `start_check` | tidak | cara verifikasi proses hidup (default: identity scan via `/proc`, lihat §2g) |
| `reload` | tidak | live-reload command. Dipakai sebelum `stop`+`start` kalau config berubah |
| `config_link` | ya untuk `bar`/`shell`/`locker`/`wallpaper` | path target symlink yang di-swap ke snapshot rice (lihat §2f untuk aturan) |
| `depends_on` | tidak | list nama komponen yang harus start lebih dulu |
| `health_check` | tidak | command exit-0 = healthy. Dijalankan setelah start dengan timeout |
| `optional` | tidak | kalau `true`, skip kalau binary tidak ada (default `false` = abort switch) |
| `timeout` | tidak | integer detik; default 10s. Untuk start/stop |
| `match` | tidak | rule untuk `launcher`/`terminal` — opsional, tidak dipakai di MVP |

**Top-level rice fields** (di luar tabel `[[components]]`):

| Field | Wajib | Arti |
|---|---|---|
| `[meta].name` | ya | nama rice, unik |
| `[meta].description` | tidak | |
| `[meta].author` | tidak | |
| `[meta].parent` | tidak | nama rice parent (untuk inheritance) — **MVP: belum dipakai**, field di-skip saat parsing |
| `wallpaper` | tidak | path wallpaper; kalau ada, run `matugen image` setelah swap (§2b) |
| `config_dir` | tidak | path relatif ke `rice.toml`; default `"."` |

**`[theme]` table** (semua opsional):

| Field | Wajib | Arti |
|---|---|---|
| `cursor_name` | tidak | theme cursor; di-apply via `hyprctl setcursor <name> <size>` dan `gsettings set org.gnome.desktop.interface cursor-theme` |
| `cursor_size` | tidak | integer; di-apply via `hyprctl setcursor <name> <size>` dan `gsettings set ... cursor-size` |
| `gtk_theme` | tidak | `gsettings set org.gnome.desktop.interface gtk-theme` |
| `icon_theme` | tidak | `gsettings set org.gnome.desktop.interface icon-theme` |
| `font_main` | tidak | tulis ke `~/.config/illogical-impulse/config.json:appearance.fonts.main` (kalau QS dipakai) |
| `font_mono` | tidak | sama, `appearance.fonts.monospace` |
| `font_size` | tidak | sama, numeric |
| `apply` | tidak | custom command post-switch untuk apply theme (§2b) |

**`[hooks]` table**:

| Field | Wajib | Arti |
|---|---|---|
| `pre` | tidak | list command, run sequential sebelum swap |
| `post` | tidak | list command, run sequential setelah swap sukses |

**`[run]` table** (path overrides, untuk testability §2e):

| Field | Wajib | Arti |
|---|---|---|
| `config_home` | tidak | override `XDG_CONFIG_HOME` (default `$XDG_CONFIG_HOME` atau `~/.config`) |
| `state_home` | tidak | override `XDG_STATE_HOME` (default `~/.local/state`) |
| `rice_home` | tidak | override root rice dir (default `~/rice` atau `RICE_HOME` env var) |

**Field yang DIHAPUS di revisi #2**:
- `exclusive_group` — diganti konsep **Managed Set** (lihat §2c). Komponen yang namanya muncul di `[[components]]` rice target = harus aktif; yang tidak = dimatikan. Tidak perlu grouping.
- `shared` — tidak dipakai. Komponen yang tidak boleh di-manage (`gnome_keyring`, `geoclue`, `ydotool`, `cliphist_*`) **TIDAK didaftarkan** di rice; mereka auto-exclude dari managed set dan tetap di-start dari `hyprland/execs.lua`.

### 2.1 Pemetaan Komponen

**[verified]** = ada di source (config / proses / sistem). **[inferred]** = disimpulkan dari struktur tapi belum diuji.

| Komponen | type | start | stop | reload | config_link | depends_on | health_check | Verifikasi |
|---|---|---|---|---|---|---|---|---|
| `hyprland` | compositor | `true` (noop) | `true` (noop) | `hyprctl reload` | `~/.config/hypr` (parent, agar `hyprland.lua`, `hyprland/`, `custom/`, `hypridle.conf`, `hyprlock.conf`, `hyprlock/` ikut ter-swap — lihat §2f) | — | `hyprctl version` exit 0 | **verified**: ada di processes, `hyprland.lua` ada, reload dipanggil `keybinds.lua:56` |
| `quickshell_ii` | shell | `qs -c ii` | `pkill -x qs` (lalu `pkill -x quickshell` fallback) | (built-in via ReloadPopup.qml + `killall ydotool qs quickshell; qs -c $qsConfig &` di keybinds.lua:56) | `~/.config/quickshell` (parent, agar `ii/` dan config lain bisa di-swap) | — | `qs -c ii ipc show` exit 0 | **verified**: PID 3208, file ada, IPC kerja |
| `waybar` | bar | `systemctl --user start waybar.service` | `systemctl --user stop waybar.service` | `systemctl --user reload waybar.service` (= SIGUSR2 ke MAINPID) | `~/.config/waybar` | — | `systemctl --user is-active waybar.service` | **verified**: template unit `/usr/lib/systemd/user/waybar.service`, `ExecReload=kill -SIGUSR2 $MAINPID` (waybar SIGUSR2 default = reload) |
| `mako` | notifications | `systemctl --user start mako.service` | `systemctl --user stop mako.service` | `makoctl reload` (ExecReload di unit) | `~/.config/mako` | — | `pgrep -x mako` | **verified**: template unit, `ExecReload=/usr/bin/makoctl reload` |
| `hypridle` | system | `hypridle` (dari hl.exec_cmd di `execs.lua:11`) | `pkill -x hypridle` | SIGHUP | (ikut `~/.config/hypr` parent — §2f) | — | `pgrep -x hypridle` | **verified**: PID 3219, command persis `hypridle` |
| `hyprlock` | locker | `hyprlock` (saat lock, dipanggil hypridle `lock_cmd`) | `pkill -x hyprlock` | SIGHUP | (ikut `~/.config/hypr` parent) | hypridle | `pgrep -x hyprlock` (saat lock only) | **verified**: `hypridle.conf:1` pakai quickshell IPC; `hyprlock` tidak running saat ini (normal) |
| `hyprsunset` | color_daemon | `pidof hyprsunset \|\| hyprsunset` (Quickshell `Hyprsunset.qml:87`) | `pkill -x hyprsunset` | `hyprctl hyprsunset temperature N` | — | — | `pgrep -x hyprsunset` | **verified**: PID 4722 PPID=1 (Quickshell spawn) |
| `wallpaper_qs` | wallpaper | n/a (Quickshell gambar sendiri, bukan daemon) | n/a | `qs -c ii ipc call wallpapers apply <path>` (untuk re-theme in-place setelah switch) | `~/.config/illogical-impulse/config.json:background.wallpaperPath` | quickshell_ii | IPC ack | **inferred**: wallpaper dir tidak ada daemon terpisah; QS render sendiri |
| `awww` (alt) | wallpaper | `awww-daemon` (manual, bukan autostart) | `awww kill` | `awww img <path>` | `~/.config/awww/current_wallpaper.txt` | — | `pgrep -x awww-daemon` | **verified**: binary ada, **tidak running**, hanya fallback kalau mau pakai daemon |
| `matugen` | theme_generator | n/a (jarang manual) | n/a | `matugen image <path>` (re-render templates) | `~/.config/matugen/config.toml` | — | `command -v matugen` | **verified**: binary, config ada |
| `kde_material_you` | color_daemon | n/a (dipanggil dari `switchwall.sh`) | n/a | (re-trigger via matugen) | `~/.config/kde-material-you-colors/config.conf` | matugen | `command -v kde-material-you-colors` | **verified**: binary & config |
| `easyeffects` | system (audio) | `easyeffects --hide-window --service-mode` (di `execs.lua:16`) | `pkill -x easyeffects` | — | — | — | `pgrep -x easyeffects` | **inferred**: NOT INSTALLED, baris autostart silent fail; **wajib `optional = true`** |
| `gnome_keyring` | system (auth) | `gnome-keyring-daemon --start --components=secrets` | (jangan matikan — handle via session) | — | — | — | `pgrep -x gnome-keyring-daemon` | **verified**: PID 2502, start di `execs.lua:10`; **TIDAK di-manage** — tetap di `execs.lua` |
| `cliphist_text` | clipboard | `wl-paste --type text --watch bash -c 'cliphist store && qs -c ii ipc call cliphistService update'` | `pkill -x wl-paste` | — | — | quickshell_ii | `pgrep -x wl-paste` | **verified**: PID 3234, start di `execs.lua:20`; **TIDAK di-manage** — tetap di `execs.lua` |
| `cliphist_image` | clipboard | `wl-paste --type image --watch bash -c 'cliphist store && qs -c ii ipc call cliphistService update'` | `pkill -x wl-paste` | — | — | quickshell_ii | `pgrep -x wl-paste` | **verified**: PID 3235, start di `execs.lua:21`; **TIDAK di-manage** — tetap di `execs.lua` |
| `fuzzel` | launcher | `fuzzel` (fallback dari keybind, bukan autostart) | `pkill -x fuzzel` | — | `~/.config/fuzzel` | — | `pgrep -x fuzzel` (saat run only) | **verified**: binary, config matugen-linked |
| `rofi` | launcher (alt) | `rofi -show drun` | `pkill -x rofi` | — | `~/.config/rofi` | — | `pgrep -x rofi` (saat run only) | **verified**: binary |
| `wlogout` | system (session menu) | `wlogout -p layer-shell` (keybind `CTRL+ALT+DEL`) | `pkill -x wlogout` | — | `~/.config/wlogout` | — | `pgrep -x wlogout` (saat run only) | **verified**: binary, keybind |
| `kitty` | terminal | `kitty -1` (dari `launch_first_available.sh`) | `pkill -x kitty` | SIGHUP | `~/.config/kitty` | — | n/a (user-triggered) | **verified**: binary |
| `fish` | shell | (login shell) | n/a | (config.fish di-source) | `~/.config/fish` | — | `command -v fish` | **verified**: fish binary |
| `starship` | system (prompt) | di-source dari `fish/config.fish:14` | n/a | (terminal restart) | `~/.config/fish/starship.toml` (overrides `~/.config/starship/starship.toml`) | fish | n/a | **verified**: binary, config ada |
| `cava` | system (audio visualizer) | (jarang, dari QS panel) | `pkill -x cava` | — | `~/.config/cava/` | — | `pgrep -x cava` (saat run only) | **verified**: dir ada (10K), binary `/usr/bin/cava` |
| `cursor` | system (theme) | `hyprctl setcursor <name> <size>` (di `execs.lua:24`) | n/a | (re-apply via `hyprctl setcursor`) | — (tidak ada file) | — | `hyprctl getoption cursor:theme` | **verified**: command persis di execs.lua |
| `geoclue` | system | `~/.config/hypr/hyprland/scripts/start_geoclue_agent.sh` | `pkill -x geoclue-2.0` | — | — | — | `pgrep -x geoclue-2.0` | **verified**: PID 3238, script di `execs.lua:5`; **TIDAK di-manage** — tetap di `execs.lua` |
| `ydotool` | system | (auto via `ydotool.service`, enabled) | n/a | n/a | — | — | `pgrep -x ydotoold` | **verified**: PID 613, service enabled; **TIDAK di-manage** — systemd |

### 2.2 Catatan Penting dari Pemetaan (revisi)

1. **Managed set = union of all rice components.** Yang TIDAK masuk rice (`gnome_keyring`, `geoclue`, `ydotool`, `cliphist_*`) tetap start dari `execs.lua` dan **tidak pernah disentuh** oleh switcher. Lihat §2c.
2. **Quickshell `ii` = 1 monolithic shell.** Tidak ada pemisahan proses internal; switch `shell` artinya stop+start `qs -c ii`. Symlink target = `~/.config/quickshell` (parent, agar bisa swap `ii/`, `minimal/`, dll).
3. **`hyprland` swap = parent dir.** `hyprland.lua` + `hyprland/` + `custom/` + `hypridle.conf` + `hyprlock.conf` + `hyprlock/` di-swap atomik. `hypridle.conf` dan `hyprlock.conf` **tidak** didaftarkan sebagai `config_link` sendiri (lihat §2f).
4. **`easyeffects` adalah baris autostart yang saat ini silent fail** (binary tidak ada). Manifest wajib `optional = true`. Kalau tidak, switch abort.
5. **`awww-daemon` TIDAK running**. Matugen + Quickshell render wallpaper, bukan daemon. Untuk rice yang mau pakai `awww`, user tambahkan sendiri ke manifest; `awww-daemon` start manual, bukan dari `execs.lua`.
6. **Cursor theme inkonsisten di setup saat ini** (bug, bukan tanggung jawab switcher): `Bibata-Modern-Classic 24` di hyprland vs `Bibata-Modern-Ice 20` di `settings.ini`/`xsettingsd.conf`. Setelah switch, switcher apply **keduanya** supaya konsisten. Rice yang override `theme.cursor_name`/`cursor_size` akan menormalkan ke nilai rice.
7. **Matugen outputs via symlink**: file output matugen (lihat §1.7) akan ditulis ke path yang saat ini symlink ke folder rice. Ini **mengotori repo** (rice/<name>/hyprland/colors.lua, dll.). **Solusi**: masukkan path matugen-generated ke `.gitignore` (lihat §2b).
8. **`hyprland.lua` adalah file Lua.** Swap seluruh `~/.config/hypr/` direktori (atomic rename). Reload via `hyprctl reload`. Validasi pre-swap via `hyprland --verify-config --config <rice_target>/hyprland.lua` (dikonfirmasi mendukung `--config <path>`).
9. **HyDE leftovers**: `wallbash/`, `niri.service.wants/`, `niri.sh` di wallbash, waybar `niri/workspaces` — semua sisa percobaan niri. **Tidak dipakai** oleh setup aktif. Rice example B akan replace waybar `left.json` ke `hyprland/workspaces` (lihat §1.4).

---

## 2b. Wallpaper & Theming (revisi)

**Keputusan (override §9.1 #4 lama)**: switcher memanggil **`matugen image <wallpaper>` langsung**, **TIDAK** lewat Quickshell `wallpapers apply` IPC.

Alasan:
1. IPC `wallpapers apply` butuh `qs -c ii` running. Kalau `quickshell_ii` masuk managed set (kemungkinan besar), dia di-stop di langkah 6 sebelum langkah 11 (matugen). Urutan jadi salah.
2. `matugen image` adalah CLI murni; jalan tanpa state.
3. `switchwall.sh` yang dipanggil IPC punya side-effect tambahan (`pkill mpvpaper`, `gsettings set color-scheme`, spawn `kde-material-you-colors-wrapper.sh`). Switcher panggil `matugen` langsung; efek-efek tambahan itu di-relay ke hook kalau perlu (`theme.apply` di manifest, atau `hooks.post`).

**`theme.apply` field** (override per rice): optional command yang di-run setelah `matugen` (atau sebagai gantinya). Contoh untuk rice `ii`:
```toml
[theme]
apply = "~/.config/quickshell/ii/scripts/colors/switchwall.sh"
# Args: passed by switcher sebagai "<mode> <wallpaper>"
# mode = "dark" | "light" (auto-detect dari wallpaper bright/dark atau dari state)
```

**Matugen outputs & `.gitignore`**: file yang dihasilkan matugen ditulis lewat symlink ke dalam folder rice. Contoh path:
- `rice/<name>/hypr/hyprland/colors.lua`
- `rice/<name>/hypr/hyprlock/colors.conf`
- `rice/<name>/fuzzel/fuzzel_theme.ini`
- `rice/<name>/gtk-3.0/gtk.css`
- `rice/<name>/illogical-impulse/config.json` (kalau ada wallpaperPath field)

Ini **mengotori repo** kalau di-commit. Solusi: tambahkan ke `.gitignore`:
```
# Matugen-generated
**/hyprland/colors.lua
**/hyprlock/colors.conf
**/fuzzel/fuzzel_theme.ini
**/gtk-3.0/gtk.css
**/illogical-impulse/config.json
**/user/generated/colors.json
**/user/generated/wallpaper/path.txt
```
File-file ini di-regenerate tiap switch wallpaper; tidak boleh di-commit. README menjelaskan bahwa rice pertama kali setelah clone butuh `matugen image` untuk populate file-file ini.

---

## 2c. Managed Set & Compositor (revisi #3)

**Definisi**:
- `managed_set` = union dari **identitas** komponen di semua rice (lihat §2g untuk definisi identitas).
- Saat switch: komponen di `managed_set ∩ target_rice.components` (identity match) = **aktif** (start/reload); di `managed_set ∖ target_rice.components` = **dimatikan**.
- Komponen TIDAK di `managed_set` = **tidak pernah disentuh** oleh switcher.

**Aturan tambahan (revisi #3)**:

1. **Komponen bertipe `compositor` tidak masuk `to_stop` atau `to_start`**. Hyprland adalah satu-satunya compositor di setup ini; dia di-swap via `config_link` + `hyprctl reload`, bukan stop+start. Invariant: `to_stop ∩ compositor_types = ∅` dan `to_start ∩ compositor_types = ∅`. (Diverifikasi di `SwitchAlgorithmTests::test_compositor_never_in_to_stop_or_to_start`.)

2. **Komponen target yang sudah berjalan dan konfigurasinya tidak berubah dipertahankan tanpa restart**. Termasuk kasus di mana `qs -c ii` di rice A dan `qs -c minimal` di rice B keduanya declare komponen dengan nama "qs" tapi **identitas berbeda** (lihat §2g). Algoritma identity-aware membedakannya.

3. **Komponen target dengan konfigurasi berubah di-reload kalau `reload` field ada, else di-restart**. Default urutan preferensi: reload > restart. Komponen yang tidak punya `reload` (mis. `qs -c ii` → `qs -c minimal`, argv berbeda, tidak ada cara reload) otomatis masuk to_stop+to_start.

4. **Komponen lama di luar rice target dimatikan secara terkontrol**: `stop_check` ditunggu dengan timeout 5s; kalau gagal, `SIGTERM` lalu `SIGKILL` (lihat §5.2).

**Yang TIDAK masuk managed set** (auto-exclude, tetap di-start dari `hyprland/execs.lua`):
- `gnome_keyring` (auth dependency)
- `geoclue` (location service)
- `ydotool` (systemd user service, enabled)
- `cliphist_text`, `cliphist_image` (clipboard infrastructure, Quickshell IPC coupled)

Alasan exclude: komponen-komponen ini **bukan estetika**; mereka auth/clipboard/lokasi yang harus selalu hidup selama Hyprland session. Matikan = kehilangan state (clipboard history, geoclue wifi positioning, keyring unlock).

**Counter-example**: `hyprsunset` ada di managed set (kadang dimatikan, mis. rice untuk screenshot tanpa blue light filter). `hypridle` & `hyprlock` tercakup di swap `~/.config/hypr` parent (tidak di daftar sebagai komponen terpisah).

**`shared` field**: **dihapus**. Cukup dengan managed set convention.

**Contoh transisi** (lengkap di §10 test_design.py):

1. **Quickshell `ii` → Quickshell `minimal`**: identity `("qs", ("-c", "ii"))` di to_stop, identity `("qs", ("-c", "minimal"))` di to_start, `to_swap` berisi `~/.config/quickshell`, hyprland di to_keep (compositor).
2. **Quickshell → Waybar**: identity `("qs", ())` di to_stop, identity `("waybar", ())` di to_start, `to_swap` berisi `~/.config/{hypr,quickshell,waybar}`, hyprland di to_keep.
3. **Waybar → Quickshell**: identity `("waybar", ())` di to_stop, identity `("qs", ())` di to_start, `to_swap` berisi `~/.config/{hypr,quickshell,waybar}`, hyprland di to_keep.
4. **Dua rice share komponen** (mis. `mako` di A dan B): identity `("mako", ())` di to_keep, tidak di to_stop / to_start / to_reload.
5. **Rice target gagal dijalankan setelah komponen lama dihentikan**: rollback re-derive plan dari current rice (yang sudah sebagian switch) ke rice target-asal. `to_stop` berisi komponen baru yang gagal health check; `to_start` berisi komponen lama yang harus dihidupkan kembali. Diverifikasi di `FailureRollbackTests::test_plan_only_includes_components_in_target_rice`.

**Field yang DIHAPUS di revisi #3**:
- `name`-based stop/start keputusan diganti identity-based (lihat §2g). Konsekuensi: dua rice dengan logical name "qs" tapi argv berbeda tidak dianggap komponen yang sama.

---

## 2d. Lifecycle Autostart (revisi #3 — menjawab prompt §6)

**Tujuan**: hapus baris `hl.exec_cmd("qs -c $qsConfig")`, `hl.exec_cmd("hypridle")`, `hl.exec_cmd("pkill ... mako")`, `hl.exec_cmd("pkill ... fuzzel")`, `hl.exec_cmd("pkill ... wlogout")`, `hl.exec_cmd("hyprctl setcursor ...")` dari `hyprland/execs.lua`. Ganti dengan satu baris:

```lua
hl.on("hyprland.start", function ()
    hl.exec_cmd("~/.local/bin/rice-switcher apply")
end)
```

`rice-switcher apply` = restore rice terakhir dari `state.json` (start komponen yang harus aktif sesuai managed set).

**Verifikasi lifecycle `hl.on("hyprland.start")` (revisi #3)**:

- **Diverifikasi dari sumber**: Hyprland Wiki, *Configuring → Advanced-and-Cool → Expanding-functionality* (https://wiki.hypr.land/Configuring/Advanced-and-Cool/Expanding-functionality/), tabel `hl.on` events:
  - `hyprland.start` → *"Emitted once on start"*
  - `config.reloaded` → *"Emitted when the config has been reloaded and applied"*
  - (Ada banyak event lain: `window.open`, `workspace.created`, dll, tapi yang relevan untuk autostart adalah `hyprland.start`.)
- **Batasan sumber**: kutipan di atas dari Hyprland Wiki (sumber komunitas, bukan source resmi). Source code `hl.on` ada di `hyprland/services/init.lua` di rice user (logical implementation). Untuk release-cut Hyprland, recommend verifikasi lebih lanjut: cek ABI Hyprlang 0.6.8 (yang dipakai di setup ini) atau jalankan probe `hl.on("hyprland.start", log)` di test session dan `hyprctl reload` untuk konfirmasi.
- **Implikasi**: `hl.on("hyprland.start", ...)` callback **hanya fires sekali** saat Hyprland session start. Aman dari double-start.
- **Top-level `exec` di Hyprlang re-runs on reload** (dikonfirmasi dari commit log BlueManCZ/hyprland-config 0.6.6: *"`exec` emits at top level (re-runs on reload, matching Hyprlang's semantics); `exec-once` stays inside `hl.on` so its callback fires only at session startup"*). Implikasi: `rice-switcher apply` HARUS dipanggil dari dalam `hl.on("hyprland.start", ...)`, **TIDAK** sebagai top-level `exec`. Kalau dipanggil sebagai top-level `exec`, dia akan fire setiap `hyprctl reload`.

**Verifikasi Hyprlang versi 0.6.8 kompatibilitas dengan setup ini**:
- `hyprctl version` di setup: `Hyprland 0.56.2 built from branch v0.56.2 at commit efb5099...`. Hyprlang ABI: `hlg_0.6`.
- Hyprland 0.56.x ships with Hyprlang 0.6.8 (lihat `hyprlang` field di version output). Konfirmasi bahwa `hl.on("hyprland.start")` API stabil di 0.6.8: **tidak** dilakukan di revisi ini (tidak ada source Hyprlang 0.6.8 yang tersedia secara offline). Marked **belum diverifikasi untuk release**. Untuk MVP: trust Hyprland Wiki, validasi runtime nanti.

**Yang TETAP di `execs.lua`** (tidak di-manage oleh switcher):
- `gnome-keyring-daemon --start --components=secrets` (line 10)
- `dbus-update-activation-environment --all` (line 12-13)
- `wl-paste --type text --watch ...` (line 20)
- `wl-paste --type image --watch ...` (line 21)
- `start_geoclue_agent.sh` (line 5) — geoclue auto-exclude dari managed set
- `easyeffects --hide-window --service-mode` (line 16) — optional, binary belum ada, tapi tetap di execs.lua agar symlink rice yang include easyeffects tidak bentrok
- `hyprctl setcursor ...` (line 24) — boleh tetap di execs.lua sebagai default awal, lalu di-override oleh `[theme]` rice saat switch

**Risiko**: lifecycle `hl.on` di Lua config adalah **extension Hyprland ke Hyprlang**, bukan Hyprlang core. Perubahan minor di Hyprland API bisa break ini. Untuk MVP, accept risk; Slice 3+ tambah probe di `rice-switcher apply` yang fail-fast kalau Hyprland session tidak punya `hl.on` callback.

---

## 2e. Path Overrides & Mode Test (revisi)

**Path roots** (lihat §2.0 `[run]` table):
- `RICE_HOME` env var (default `~/rice`): root rice directory
- `XDG_CONFIG_HOME` (default `~/.config`): target symlink
- `XDG_STATE_HOME` (default `~/.local/state`): state, snapshot, lock

**Mode test** (env `RICE_TEST=1`):
- Semua path di-override ke `/tmp/rice-test-XXXX/`
- `PATH` di-prepend dengan stub directory berisi script-script imitasi:
  - `pkill` → exit 0, log ke `$RICE_TEST_LOG/pkill.log`, tulis PID fake ke `$RICE_TEST_PIDS/<name>`
  - `systemctl` → exit 0
  - `hyprctl` → exit 0 + emit "config ok" untuk `verify-config`
  - `hyprland` → exit 0
  - `qs` → exit 0 + emit "instance started" untuk `ipc show`
  - `matugen` → exit 0 + tulis fake file ke matugen output paths
  - `pgrep` → baca `$RICE_TEST_PIDS/<name>`, exit 0 kalau ada, 1 kalau tidak
- Stub functions tersedia via Python module `tests.stubs` (juga dipanggil dari Python tests, bukan cuma shell)
- `RICE_TEST=1` + `RICE_TEST_PIDS=/tmp/pids` → full hermetic test

**Test cases minimum** (sebelum Slice 1 "done"):
- `test_init_creates_state_file`
- `test_validate_schema_invalid_returns_error`
- `test_validate_schema_valid_returns_ok`
- `test_validate_hyprland_lua_uses_verify_config`
- `test_validate_missing_binary_aborts`
- `test_validate_optional_binary_does_not_abort`
- `test_switch_quickshell_to_waybar` (dengan stub)
- `test_switch_back_waybar_to_quickshell`
- `test_double_switch_aborts_via_lock`
- `test_rollback_restores_symlinks`
- `test_managed_set_excludes_non_managed_components`
- `test_matugen_rewrite_gitignored`

---

## 2f. Aturan `config_link` (revisi #3 — menjawab prompt §4)

**Aturan 1 — Tidak ada `config_link` yang overlap** (revisi):

Setelah normalisasi (`os.path.abspath + os.path.expanduser`, **TANPA** realpath — see Aturan 6), dua config_link konflik jika:

- **Identik**: `A == B`
- **Parent/child**: `A ⊂ B` atau `B ⊂ A` (strict prefix + path separator)
- **Symlink-equivalent**: setelah `realpath` (hanya untuk deteksi, bukan untuk menyimpan), `realpath(A) == realpath(B)`. Contoh: A = `/foo/bar` (symlink ke `/baz`), B = `/baz` → konflik (A adalah symlink ke B).
- **Relative + absolute equivalent**: `~/foo` dan `/home/USER/foo` adalah path yang sama untuk user `USER`; cek setelah `abspath + expanduser`.

**Diverifikasi** di `ConfigLinkConflictTests`:
- `test_identical_paths_detected` — path identik
- `test_nested_child_inside_parent_detected` — `~/.config/hypr` dan `~/.config/hypr/hyprland` → konflik
- `test_hypridle_conf_under_hypr_parent_is_conflict` — `~/.config/hypr` dan `~/.config/hypr/hypridle.conf` → konflik (Aturan 2)
- `test_path_normalisation_handles_relative_and_tilde` — `~/` dan `/home/USER/` cocok

**Aturan 2 — `hypridle.conf` & `hyprlock.conf` tidak boleh didaftarkan terpisah**:
- Karena tercakup dalam parent `~/.config/hypr` swap.
- Validate deteksi `~/.config/hypr/hypridle.conf ⊂ ~/.config/hypr` → abort.
- Rice yang ingin override `hypridle.conf` saja: harus include parent `~/.config/hypr` di swap (semua file `hyprland/`, `custom/`, dll ikut). Atau pisah rice dengan rice A = parent swap, rice B = tidak swap hyprland sama sekali (hanya `quickshell`).

**Aturan 3 — `hyprland.lua` ikut swap parent**:
- `hyprland.lua` ada di `~/.config/hypr/` (bukan di `hyprland/` subdir). Swap parent `~/.config/hypr` mengikutkan `hyprland.lua` + `hyprland/` + `custom/` + `hypridle.conf` + `hyprlock.conf` + `hyprlock/`.
- Rice yang **hanya** swap `hyprland/` subdir TIDAK valid: `hyprland.lua` tidak ikut, dan config tidak akan load.
- Validate: kalau ada `config_link` di rice = `~/.config/hypr/hyprland` dan rice B TIDAK swap `~/.config/hypr`, → warning "config mungkin incomplete".

**Aturan 4 — `~/.config/gtk-4.0` dangling symlink** (lihat Lampiran B):
- Symlink ke `~/.local/share/themes/Wallbash-Gtk/gtk-4.0` (target **tidak ada**).
- `nwg-look/config:export-gtk4-symlinks=false` — GTK4 default theme.
- Matugen template `gtk-4.0/gtk.css` ada, tapi output `~/.config/gtk-4.0/gtk.css` akan fail (parent dir symlink, target missing).
- **Rekomendasi**: hapus symlink `~/.config/gtk-4.0` atau `mkdir -p ~/.local/share/themes/Wallbash-Gtk/gtk-4.0` di pre-install. Keputusan user (lihat §9.2).

**Aturan 5 — Path yang belum ada tetap diizinkan**:
- `os.path.lexists(config_link)` → True (lexists menerima dangling).
- Validate TIDAK abort kalau `config_link` belum ada di filesystem; dia akan dibuat saat switch.
- Tapi: validate **warning** kalau `config_link` bukan symlink dan tidak ada di rice dir target → "config_link not found, will create".

**Aturan 6 — `realpath` hanya untuk DETEKSI, bukan untuk menyimpan path**:
- Untuk cek konflik symlink-equivalent (§Aturan 1) dan untuk deteksi target rice (`/TEST-RICE/<name>`), `realpath` dipakai.
- `realpath` TIDAK dipakai untuk normalisasi `config_link` itu sendiri, karena:
  - `realpath` mengikuti symlink — kalau `config_link` adalah symlink ke target rice, `realpath` akan return target rice path, sehingga perbandingan `realpath(config_link) == target_rice_dir` selalu True (tidak bisa detect perubahan).
  - `realpath` gagal untuk path yang tidak ada: `os.path.realpath("/nonexistent")` return `/nonexistent` unchanged. Untuk path yang BELUM dibuat (rice target belum ada), `realpath` tidak bantu.
  - Untuk path yang **harus dibuat** saat switch (mis. rice A swap ke rice B, dan `~/.config/foo` belum ada di rice B), `realpath` akan return input unchanged, sehingga perbandingan `realpath("/nonexistent") == "/TEST-RICE/B/foo"` → False, dan switcher akan swap. OK.
  - Untuk path yang **sudah ada sebagai symlink** (mis. `~/.config/gtk-4.0` → dangling), `realpath` mengikuti symlink dan resolve ke target (yang tidak ada). Untuk cek **"apakah symlink ini point ke rice target?"**, kita pakai `realpath` di satu sisi dan target rice dir di sisi lain.

**Aturan 7 — Jangan follow symlink yang escape area yang diizinkan**:
- `config_link` HARUS dimulai dengan `~/`, `~/.config/`, atau path absolut yang berada di bawah `RICE_HOME`/`XDG_CONFIG_HOME`.
- Validate: setelah normalisasi, `os.path.commonpath([config_link, RICE_HOME])` harus include config_link ATAU config_link harus di bawah XDG dirs.
- Contoh INVALID: `config_link = "/etc/passwd"` → abort. Contoh INVALID: `config_link = "/home/other-user/.config"`.

**Diverifikasi** semua aturan di `ConfigLinkConflictTests` (5 tests) + `test_dangling_symlink_target_not_followed` (realpath behaviour test).

---

## 2g. Identitas Proses (revisi #3 — menjawab prompt §3)

**Definisi identitas**: identitas sebuah komponen adalah tuple `(executable_basename, tuple(argv_match))`. Dua komponen dianggap "proses yang sama" hanya jika tuple identik.

**Field tambahan per komponen** (di `[[components]]`):

| Field | Wajib | Arti |
|---|---|---|
| `executable` | tidak (default = `name`) | basename executable di `/proc/<pid>/comm` atau argv[0] basename. Mis. untuk Quickshell: `"qs"`. |
| `argv_match` | tidak (default = `[]`) | list token yang HARUS ada di argv proses (urutan tidak penting, semua harus match). Mis. untuk `qs -c ii`: `["-c", "ii"]`. |
| `exact_argv` | tidak (default = `false`) | bila `true`, mode match berubah jadi **exact**: argv proses (setelah executable) harus sama persis dengan `argv_match` — jumlah, isi, dan urutan. `exact_argv=true` + `argv_match=[]` = identitas **tanpa argumen**. Lihat "Identitas tanpa argumen" di bawah. |

**Mengapa perlu lebih dari basename**: dua Quickshell (`qs -c ii` dan `qs -c minimal`) memiliki executable basename `"qs"` yang **sama**. Membedakan mereka dengan basename saja akan salah identity — algoritma akan menganggap `qs -c minimal` (baru) sama dengan `qs -c ii` (lama) dan tidak melakukan apa-apa. `argv_match` adalah disambiguator.

**Identitas tanpa argumen (`exact_argv`, Fase 2b Langkah 1).** Daemon yang tidak menerima argumen (mis. `waybar`, `hypridle`, `mako`, `dunst`) tidak punya token `argv_match` yang bisa dipakai untuk membedakannya — subset match dengan `argv_match=[]` terlalu luas karena cocok dengan **semua** proses yang basename-nya sama (termasuk `waybar -c x`). Karena itu `terminate_by_identity` menolak `argv_match` kosong. Untuk memberi daemon seperti itu identitas yang bisa dihentikan, manifest boleh menyatakan secara eksplisit:

```toml
[[components]]
name = "waybar"
type = "bar"
executable = "waybar"
exact_argv = true      # identitas: argv tepat ["waybar"], tanpa argumen
stop = ""              # stop berbasis identitas (§4.2)
```

Aturan:
- `exact_argv=true` mengubah match menjadi **equality** (bukan subset): `argv_proses == argv_match` dan `basename(argv[0]) == executable`. Urutan penting; tidak boleh ada token tambahan **atau** kurang.
- `exact_argv=true` + `argv_match=[]` hanya cocok dengan proses yang argv-nya **satu elemen** (executable itu saja). Ini **bukan** basename-only: `waybar -c x` tidak ikut cocok.
- `argv_match=[]` **tanpa** `exact_argv` tetap **error** — tidak ada arti implisit "tanpa argumen".
- Manifest lama yang memakai `argv_match` (subset) **tidak berubah perilakunya**: `exact_argv` default `false`, dan semua guard lama (uid sendiri, bukan PID 1/diri/parent, baca ulang `/proc/<pid>/cmdline` sebelum sinyal, SIGTERM→tunggu→SIGKILL, compositor ditolak) tetap berlaku.

**Algoritma scan `/proc/*/cmdline`** (PLAN.md §2g):
```python
def find_pids_by_identity(executable, argv_match, exclude_self_pid):
    out = []
    self_pid = os.getpid()
    ppid = os.getppid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit(): continue
        pid = int(entry)
        if pid in (self_pid, ppid, exclude_self_pid): continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                parts = f.read().split(b"\0")
                parts = [p for p in parts if p != b""]
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if not parts: continue
        exe = os.path.basename(parts[0].decode(errors="replace"))
        if exe != executable: continue
        argv_strs = [p.decode(errors="replace") for p in parts[1:]]
        if all(token in argv_strs for token in argv_match):
            out.append(pid)
    return out
```

**Normalisasi argv**: hapus trailing NUL (`\0`). `argv[0]` adalah path executable; basename-nya yang dicocokkan. Token sisanya adalah argv.

**Mencegah false positive**:
- `pgrep -x <basename>` (jika dipakai sebagai fallback) match persis command name (15 char limit di Linux), BUKAN argv. Tidak cukup untuk Quickshell.
- `pkill -f` match SELURUH cmdline → **dilarang** (akan match `bash -c "qs ..."` dan rice-switcher itu sendiri).
- Scan `/proc` membandingkan basename argv[0] + subset argv. Token `argv_match` bersifat **subset match** (semua token harus ada), bukan equality. Ini agar `qs -c ii --foo` masih di-match oleh `argv_match=["-c", "ii"]`.

**Ownership proses**: switcher **tidak mengklaim ownership** proses yang sudah berjalan. Prinsip: proses yang ditemukan di `/proc` adalah "active" terlepas dari siapa yang start. Jika proses itu BUKAN dari rice manapun (orphan), switcher **tidak** men-stop-nya; dia akan masuk ke `to_stop` hanya jika identity-nya ada di managed_set ∩ current rice tapi tidak di target rice.

**Perilaku saat proses target sudah berjalan tapi tidak dimiliki switcher**:
- `find_pids_by_identity` (eksperimen): list PIDs. Switcher **tidak** mengirim signal; dia hanya membaca.
- Untuk stop: `kill <pid>` ke PID yang ditemukan. Kalau proses itu BUKAN yang kita kira (race: PID di-reuse), signal akan dikirim ke proses yang salah. Mitigasi: re-check PID existence + comm sebelum kill; abort kalau beda.
- Untuk start: switcher spawn proses baru dan **tidak** mengasumsikan PID tertentu. Dia akan menemukan PID baru via scan ulang setelah start.
- **Tidak ada "claim"** — proses dianggap miliknya hanya selama switch window; setelah release lock, semua proses adalah "active" (siapa pun yang start).

**Batasan yang masih belum dipecahkan** (perlu diskusi dengan your-name):
- Kalau ada 2 `qs -c ii` berjalan paralel (kasus langka), `kill <pid>` akan membunuh satu. Switcher tidak punya cara menentukan yang mana tanpa info tambahan (mis. PID file, environment, socket, dll.). Untuk MVP, terima ini sebagai keterbacaan.
- Proses yang di-spawn oleh komponen lain (mis. `hyprsunset` di-spawn oleh `Hyprsunset.qml:87` di Quickshell) bukan anak langsung switcher. Switcher bisa kill `hyprsunset` via identity match, tapi Quickshell akan respawn-nya (lihat R8). Untuk MVP, ini diterima; Quickshell akan re-init hyprsunset sesuai konfigurasi baru.

---

## 2h. Keamanan (revisi #3 — menjawab prompt §9)

**Asumsi ancaman**: rice.toml dari sumber luar (clone dari GitHub, share antar teman, dll.) menjalankan shell command di sistem user.

**Bidang berbahaya** (semua menjalankan shell command arbitrary):
- `start`, `stop`, `reload` (di `[[components]]`)
- `hooks.pre`, `hooks.post`
- `theme.apply`

**Kebijakan (revisi #3)**:

### Manifest dari rice lokal yang dipercaya
- Rice yang di-track di repo lokal dianggap "trusted" (user sudah review saat `rice add`).
- Trust TIDAK inherit otomatis ke sub-rice atau imported rice.

### `rice-switcher import <path>` (read-only, prompt §9)
- Membaca `rice.toml` (parsed via tomllib) dan path-path config_link.
- **TIDAK** mengeksekusi `start`, `stop`, `hooks`, atau `apply`.
- Hanya copy file ke `rice/<name>/` setelah validasi.
- `--dry-run` default ON; user harus `--apply` untuk benar-benar import.

### Preview & konfirmasi (prompt §9)
- `rice-switcher switch <name>` dengan rice yang **belum pernah** di-switch di sistem ini:
  - Print semua command yang akan di-run (`start`, `stop`, `hooks`, `theme.apply`, `matugen` args) dengan **path lengkap** dan **redaksi secret** (lihat audit di bawah).
  - Prompt: "Type 'yes' to continue".
  - Kalau user jawab bukan "yes": abort.
  - Setelah rice pernah di-switch di sistem ini, konfirmasi di-skip (cached di state).

### Persetujuan SEKALI tidak otomatis (prompt §9 eksplisit)
- Jika manifest rice **berubah** (mtime, sha256 berbeda dari saat persetujuan terakhir), konfirmasi diminta lagi.
- Persetujuan di-cache dengan hash manifest. Setiap switch, hash dicek; kalau beda, prompt lagi.
- Ini mencegah skenario: rice A dipercaya saat di-add, lalu di-edit (atau otomatis di-update) untuk inject malicious command; konfirmasi tidak akan skip.

### Audit (prompt §9)
- Setiap `start`/`stop`/`hook`/`apply` yang dieksekusi dicatat ke `~/.local/state/rice-switcher/audit.log` (timestamp, rice name, command, exit code).
- **TIDAK PERNAH** log token, API key, atau path yang mengandung secret.
- Audit log format JSON lines (satu entry per line, easy grep).
- Retention: keep all, gzip >30 hari. Bisa di-override via env `RICE_AUDIT_ROTATE_DAYS`.

### Sanitasi command yang di-eval (prompt §9)
- Jangan pernah eval string command. Pakai `subprocess.run([...args...])` dengan argv list.
- Hook script: spawn dengan `subprocess.Popen(["bash", "-c", hook])`. Shell injection risk ditanggung user, tapi dokumentasi jelas.
- `start`/`stop`/`reload` untuk komponen bawaan (waybar, mako, qs, hypridle, dll.) di-hardcode sebagai argv list, BUKAN shell command. User yang tulis `start = "custom-binary"` di manifest = tanggung jawab mereka.

### Validasi path manifest (prompt §9)
- `config_link` HARUS di bawah `~/.config/`, `~/.local/state/`, atau `~/rice/` (rice dir). Config_link yang escape (mis. `/etc/passwd`, `/home/other-user/.config`) → abort. (Lihat §2f Aturan 7.)
- `start`/`stop`/`hooks.pre`/`hooks.post`/`theme.apply` command TIDAK divalidasi path-nya (subprocess), tapi shell injection di-hardening via argv list.

### Timeout
- Default: 10 detik per command.
- Override: `start` / `stop` bisa specify `timeout` field (integer seconds). Default fallback 10s.
- `hooks.pre` / `hooks.post`: default 30s (boleh lebih lama untuk setup script).
- `matugen`: default 60s (wallpaper besar = lebih lama).

### Allowlist (future, bukan MVP)
- `--allowlist` di manifest: hanya izinkan command dari daftar eksplisit. MVP: warning, bukan enforcement.

---

## 2i. Validasi Config Hyprland (revisi #3)

**`hyprland --verify-config`** (sudah diverifikasi §0):
- `--verify-config` print "config ok" + exit 0 kalau valid
- `--verify-config --config <path>` **mendukung path lain** (diverifikasi dengan `~/rice-backup/20261016_215357/hypr/hyprland.lua` → exit 0, dan `/tmp/does-not-exist.lua` → error)
- **Batasan yang TIDAK tercakup** (prompt §7):
  - `--verify-config` **hanya** cek syntax file yang diberikan. Ia TIDAK cek file yang di-`require()` atau di-`source` oleh config tersebut.
  - Contoh: `hyprland/execs.lua:5` merujuk `~/.config/hypr/hyprland/scripts/start_geoclue_agent.sh`. Kalau file itu tidak ada di rice target, `hyprland --verify-config` exit 0; `hl.exec_cmd(...)` baru error saat Hyprland start.
  - Untuk MVP: trust syntax check. Pre-swap validate pakai `hyprland --verify-config --config <rice_target>/hyprland.lua`. Post-swap: `hyprctl reload` + cek `hyprctl configerrors` (read-only, tidak restart Hyprland).
- **Alternatif preflight yang lebih ketat** (Slice 3+):
  - Parse file Lua (rice target `hyprland.lua` dan semua `require`-nya) → cek setiap referenced path exist.
  - Atau: spawn hyprland dalam mode safe dengan config target (tanpa user session) — `hyprland --safe-mode --config <target> --verify-config`. (Risiko: side-effect; tidak dipakai di MVP.)
- **Jangan** jalankan config kandidat pada compositor yang sedang digunakan. Risiko: kalau config target salah syntax, `hyprctl reload` akan crash compositor (atau fallback ke default). Slice 1: pakai `hyprland --verify-config` (offline). Slice 3+: tambah cek referenced path (offline, tanpa spawn).

**Batasan yang harus didokumentasikan ke user**: validasi config yang kami lakukan di MVP (syntax-only) TIDAK menjamin Hyprland akan start dengan config baru. Risiko residual: file referenced hilang atau malformed. Mitigasi: rollback path (lihat §5) yang otomatis restore config lama kalau `hyprctl configerrors` post-reload != empty.

---

## 3. Arsitektur & Alur Switch

### 3.1 Layout Direktori

```
~/projects/rice-switcher-hyprland/
├── rice-switcher          # CLI entry (Python)
├── pyproject.toml
├── PLAN.md                # file ini
├── README.md
├── .gitignore             # sudah dibuat
├── fixtures/              # sanitized config copies (untuk uji & contoh)
│   ├── hypr/              # config hyprland saat ini (referensi, bukan sumber)
│   └── ...
├── rice/                  # user rice configs (akan dibuat saat rice pertama ditambahkan)
│   ├── illogical-impulse/ # rice A: Quickshell ii
│   │   ├── rice.toml      # manifest
│   │   ├── hyprland/      # override atau full snapshot
│   │   ├── quickshell/ii/ # override atau full snapshot
│   │   ├── waybar/
│   │   ├── gtk-3.0/
│   │   ├── fuzzel/
│   │   ├── mako/
│   │   ├── wlogout/
│   │   ├── wallpaper.png  # (opsional, bisa juga external path)
│   │   └── ...
│   └── waybar-minimal/    # rice B: Waybar + Quickshell minimal
│       ├── rice.toml
│       ├── quickshell/minimal/  # quickshell config "minimal"
│       ├── waybar/
│       └── ...
├── snapshot/              # auto-generated: snapshot config aktif SEBELUM switch (untuk rollback)
│   └── <timestamp>/
└── state.json             # last_switch, lock, dll.
```

### 3.2 Pseudocode: `rice-switcher switch <name> [--dry-run]`

**Konvensi managed set**: managed_set = union dari nama komponen di semua rice yang ada. Saat switch:
- Komponen di managed_set ∩ rice target = **aktif** (start kalau belum, reload kalau config berubah)
- Komponen di managed_set ∖ rice target = **dimatikan**
- Komponen TIDAK di managed_set = **tidak pernah disentuh** (gnome-keyring, geoclue, ydotool, cliphist_*)

```
function switch(name, dry_run=False):
    target_rice = load_toml(f"{rice_home}/{name}/rice.toml")
    current = read_state().current_rice
    managed_set = union(all_rice_components)

    # 1. VALIDATE
    validate(target_rice)               # schema; binary present; config_link paths exist; hyprland --verify-config
    for c in target_rice.components:
        if c.optional: continue
        if not binary_present(c.start): abort(f"missing binary: {c.name}")
    if target_rice has hyprland config_link:
        # pre-swap validate: hyprland supports --config <path>
        rc = run(["hyprland", "--verify-config", "--config", expand(c.config_link)])
        if rc != 0: abort("hyprland config invalid")
    for c in target_rice.components:
        if c.config_link: assert no_nesting(c.config_link, rice_root)  # §2f

    # 2. LOCK
    acquire_lock(flock(state.lock_path))  # abort kalau held

    # 3. SNAPSHOT (sebelum perubahan)
    if current exists and not dry_run:
        snapshot = create_snapshot(current)  # cp -a config_link paths
        save to {state_home}/snapshot/<ts>/

    # 4. DIFF
    active_now = inventory()              # pgrep exact / systemctl is-active per managed_set
    target_components = {c.name for c in target_rice.components}
    to_stop    = active_now ∩ managed_set - target_components
    to_start   = target_components - active_now
    to_reload  = {c.name for c in target_rice.components if c.name in active_now and config_changed(c)}
    to_swap    = {c.config_link for c in target_rice.components if c.config_link and not symlink_matches(c.config_link, rice_dir)}
    to_keep    = active_now ∩ target_components - to_reload

    if dry_run:
        print_plan(to_stop, to_start, to_reload, to_swap, to_keep)
        return 0

    # 5. HOOKS pre-switch
    for hook in target_rice.hooks.pre:
        run_in_stub_env(hook)   # stub env bisa override pkill/systemctl/matugen/hyprctl (test mode)

    # 6. STOP (urutan: dependent → independent; safe order)
    for c in topo_sort_desc(to_stop, depends_on):
        run_shell(c.stop)              # pkill / systemctl stop
        wait_for(c.stop_check, timeout=5s)
        if c.optional and stop_failed: continue
        if stop_failed: trigger_rollback(...)

    # 7. APPLY THEME FIRST (sebelum swap, untuk cursor/gtktheme instant)
    apply_theme(target_rice.theme)     # cursor_name/size, gtk_theme, icon_theme, font_*
    if target_rice.theme.apply:
        run_shell(target_rice.theme.apply)

    # 8. SWAP SYMLINKS (atomic, batched)
    swap_links(to_swap, rice_dir)       # backup current ke .bak-<ts>; ln -s rice/X target

    # 9. VALIDATE HYPRLAND (post-swap, before reload)
    if hyprland in to_swap:
        rc = run(["hyprland", "--verify-config"])
        if rc != 0: trigger_rollback(...)

    # 10. RELOAD HYPRLAND
    if hyprland in to_swap:
        run_shell("hyprctl reload")
        sleep(0.3)
        if not check_no_configerrors(): trigger_rollback(...)

    # 11. MATUGEN (langsung, bukan via QS IPC)
    if target_rice.wallpaper:
        run_shell(f"matugen image {target_rice.wallpaper}")
        # outputs masuk ke symlink target (rice/<name>/...)
        # → file .gitignored (lihat §2b)

    # 12. START (depends_on dulu)
    for c in topo_sort_asc(to_start, depends_on):
        if c.optional and not binary_present(c.start):
            log("skip optional {c.name}"); continue
        run_shell(c.start)
        wait_for(c.start_check, timeout=10s)
        if c.health_check and not check(c.health_check, timeout=5s):
            trigger_rollback(...)

    # 13. VERIFY FINAL
    sleep(2)
    for c in target_rice.components:
        if c.name not in to_keep: continue
        if not check(c.health_check or c.start_check):
            trigger_rollback(...)
    for hook in target_rice.hooks.post:
        run_in_stub_env(hook)

    # 14. UPDATE STATE
    write_state(current=name, timestamp=now)

    # 15. RELEASE LOCK
    release_lock()
    print(f"Switched to {name}")
```

**Alasan `matugen` di langkah 11 (bukan via IPC `wallpapers apply`)**:
- IPC `wallpapers apply` butuh `ii` running, dan `ii` di-stop di langkah 6 (kalau `quickshell_ii` di-managed set).
- `matugen image` adalah CLI murni, jalan tanpa state.
- `switchwall.sh` yang dipanggil `wallpapers apply` punya side-effect tambahan (`pkill mpvpaper`, `gsettings set color-scheme`); kita panggil `matugen` langsung dan pisahkan efek-efek itu ke hook kalau perlu.

### 3.3 Pseudocode: `trigger_rollback`

```
function trigger_rollback(snapshot_path, partial_state):
    for entry in to_swap (in reverse):
        if entry.current is symlink: unlink(entry.current)
        else: rm_rf(entry.current)
        restore_from_snapshot(snapshot_path, entry)
    for c in to_start: safe_stop(c)              # best effort
    for c in to_stop_old: safe_start(c)          # best effort
    notify_user(f"Switch failed; rolled back to {previous}")
    exit 1
```

### 3.4 Subcommands

| Command | Fungsi |
|---|---|
| `rice-switcher init` | pertama kali: buat dirs, state.json, snapshot/ |
| `rice-switcher list` | daftar rice yang ada |
| `rice-switcher current` | rice aktif (heuristik: cocokkan symlink config_link ke rice dirs) |
| `rice-switcher status` | table: rice × component × running × config_link target |
| `rice-switcher switch <name>` | switch (lihat §3.2) |
| `rice-switcher switch <name> --dry-run` | print plan, no changes (di Slice 1, bukan Slice 2) |
| `rice-switcher validate <name>` | validate rice.toml + binary + path + hyprland --verify-config |
| `rice-switcher snapshot` | manual snapshot config aktif (di Slice 1) |
| `rice-switcher rollback` | balik ke snapshot terakhir |
| `rice-switcher add <name>` | scaffold rice/<name>/ dengan rice.toml + copy current config |
| `rice-switcher remove <name>` | hapus rice (tidak menyentuh symlink aktif) |
| `rice-switcher random` | pilih random rice lalu switch |
| `rice-switcher menu` | TUI picker (fuzzel/rofi) |
| `rice-switcher keybind-install` | tulis `hl.bind` untuk `SUPER+SHIFT+R` ke `~/.config/hypr/custom/keybinds.lua` |
| `rice-switcher import <path>` | import existing config dir sebagai rice baru (read-only, lihat §2h) |
| `rice-switcher diff <a> <b>` | show config diff between two rice |
| `rice-switcher edit <name>` | open rice.toml di `$EDITOR` |

### 3.5 Mode Test

`RICE_TEST=1` env var mengaktifkan mode stub:
- `RICE_HOME` di-override ke direktori sementara
- `XDG_CONFIG_HOME` di-override ke root tiruan
- `pkill`, `systemctl`, `matugen`, `hyprctl`, `hyprland`, `qs` di-shadow dengan stub script di `PATH` terdepan yang exit 0 + log ke `RICE_TEST_LOG`
- Stub `pkill` simulasi kill dengan menulis ke PID file
- Stub `hyprland --verify-config` exit 0
- Stub `qs -c ii ipc show` exit 0

Test seperti `test_switch_quickshell_to_waybar` jalan di `/tmp/rice-test-XXXXX/` tanpa mengganggu desktop.

---

## 4. Skema `rice.toml` + Contoh (revisi #2)

### 4.1 Skema (TOML valid, key order: scalars → tables → arrays)

```toml
# rice.toml — manifest satu rice
# Path: <rice_home>/<name>/rice.toml
# Validated with: python3 -c 'import tomllib, sys; tomllib.loads(open(sys.argv[1]).read())'

[meta]
name = "illogical-impulse"          # required, unique
description = "..."
author = "your-name"
created = "2026-10-16"
# parent = "base"                  # MVP: belum dipakai, di-skip saat parsing

# Scalars HARUS sebelum header tabel [[components]]
config_dir = "./"                    # relatif ke rice.toml; default "."
wallpaper = "./wallpapers/ii-default.png"

[theme]
cursor_name = "Bibata-Modern-Classic"   # di-apply ke hyprctl setcursor + gsettings
cursor_size = 24                        # integer
gtk_theme = "Wallbash-Gtk"              # gsettings
icon_theme = "Tela-circle-dracula"      # gsettings
font_main = "Google Sans Flex"          # tulis ke illogical-impulse config.json
font_mono = "JetBrains Mono Nerd Font"
font_size = "11"
apply = "~/rice/illogical-impulse/scripts/apply-theme.sh"   # opsional, custom post-theme

[hooks]
pre = ["./scripts/pre.sh"]              # list, run sequential sebelum swap
post = ["./scripts/post.sh"]            # list, run setelah swap sukses

[run]                                  # opsional, untuk testability
config_home = "/tmp/test-cfg"
state_home = "/tmp/test-state"
rice_home = "/tmp/test-rice"

# [[components]] setelah semua header tabel.
# Field minimal: name, type, start, stop
# Field lain opsional sesuai §2.0.

[[components]]
name = "hyprland"
type = "compositor"
start = "true"                          # noop (sudah running)
stop = "true"                           # noop
reload = "hyprctl reload"
config_link = "~/.config/hypr"          # PARENT dir; hyprland.lua + hyprland/ + custom/ + *.conf

[[components]]
name = "quickshell_ii"
type = "shell"
start = "qs -c ii"
stop = "pkill -x qs"
stop_check = "! pgrep -x qs"
start_check = "pgrep -x qs"
config_link = "~/.config/quickshell"    # PARENT dir (ii/, minimal/, dll)
depends_on = ["hyprland"]
health_check = "qs -c ii ipc show"

[[components]]
name = "waybar"
type = "bar"
start = "systemctl --user start waybar.service"
stop = "systemctl --user stop waybar.service"
reload = "systemctl --user reload waybar.service"
start_check = "systemctl --user is-active waybar.service"
config_link = "~/.config/waybar"
depends_on = ["hyprland"]

[[components]]
name = "mako"
type = "notifications"
start = "systemctl --user start mako.service"
stop = "systemctl --user stop mako.service"
reload = "makoctl reload"
start_check = "pgrep -x mako"
config_link = "~/.config/mako"

[[components]]
name = "hyprsunset"
type = "color_daemon"
start = "pidof hyprsunset || hyprsunset"
stop = "pkill -x hyprsunset"
start_check = "pgrep -x hyprsunset"

[[components]]
name = "easyeffects"
type = "system"
start = "easyeffects --hide-window --service-mode"
stop = "pkill -x easyeffects"
optional = true                         # skip kalau binary tidak ada

# TIDAK ada: gnome_keyring, geoclue, ydotool, cliphist_*
# Mereka auto-exclude dari managed set (tidak di-define di sini)
# dan tetap di-start dari hyprland/execs.lua
```

**Hasil validasi TOML** (contoh di atas, ditulis ke `/tmp/rice-validate/schema-example.toml` lalu di-validasi):
```
$ python3 -c 'import tomllib, sys; d = tomllib.loads(open(sys.argv[1]).read()); print("OK, keys:", list(d.keys()), "components:", len(d.get("components", [])))' /tmp/rice-validate/schema-example.toml
OK, keys: ['meta', 'theme', 'hooks', 'run', 'components'] components: 6
# exit 0
```

### 4.2 Contoh Rice A: `illogical-impulse` (Quickshell-centric)

```toml
# rice/illogical-impulse/rice.toml
[meta]
name = "illogical-impulse"
description = "Quickshell ii dengan illogical-impulse suite, matugen theming, full features"

config_dir = "./"
wallpaper = "./wallpapers/sample.png"

[theme]
cursor_name = "Bibata-Modern-Classic"
cursor_size = 24
gtk_theme = "Wallbash-Gtk"
icon_theme = "Tela-circle-dracula"
font_main = "Google Sans Flex"
font_mono = "JetBrains Mono Nerd Font"

# hooks opsional; pre/post run sebelum/sesudah swap
[hooks]
pre = []
post = []

# Tidak daftarkan waybar (rice A tidak pakai waybar; managed set auto-matikan waybar)
# Tidak daftarkan rofi/wlogout (lazy launchers; tidak di-autostart)
# Tidak daftarkan gnome_keyring/geoclue/ydotool/cliphist_* (di-start dari execs.lua)

[[components]]
name = "hyprland"
type = "compositor"
start = "true"
stop = "true"
reload = "hyprctl reload"
config_link = "~/.config/hypr"

[[components]]
name = "quickshell_ii"
type = "shell"
start = "qs -c ii"
stop = "pkill -x qs"
start_check = "pgrep -x qs"
config_link = "~/.config/quickshell"
depends_on = ["hyprland"]
health_check = "qs -c ii ipc show"

[[components]]
name = "mako"
type = "notifications"
start = "systemctl --user start mako.service"
stop = "systemctl --user stop mako.service"
start_check = "pgrep -x mako"
config_link = "~/.config/mako"

# hypridle, hyprlock, hyprsunset, cursor TIDAK di sini:
# - hypridle/lock: sudah tercakup di config_link `~/.config/hypr` (parent)
# - hyprsunset: spawned oleh Quickshell Hyprsunset.qml
# - cursor: di-apply via [theme] block, bukan komponen terpisah
```

**Hasil validasi TOML**:
```
$ python3 -c 'import tomllib, sys; d = tomllib.loads(open(sys.argv[1]).read()); print("OK, keys:", list(d.keys()), "components:", len(d.get("components", [])))' /tmp/rice-validate/rice-a.toml
OK, keys: ['meta', 'theme', 'hooks', 'components'] components: 3
# exit 0
```

### 4.3 Contoh Rice B: `waybar-minimal` (Waybar + Quickshell minimal)

```toml
# rice/waybar-minimal/rice.toml
[meta]
name = "waybar-minimal"
description = "Waybar sebagai bar utama, Quickshell minimal (hanya sidebar+lock), mako notifications"

config_dir = "./"
wallpaper = "./wallpapers/minimal-dark.png"

[theme]
cursor_name = "Bibata-Modern-Classic"
cursor_size = 24
gtk_theme = "adw-gtk3-dark"
icon_theme = "Adwaita"
font_main = "JetBrainsMono Nerd Font"
font_mono = "JetBrainsMono Nerd Font"

[[components]]
name = "hyprland"
type = "compositor"
start = "true"
stop = "true"
reload = "hyprctl reload"
config_link = "~/.config/hypr"

[[components]]
name = "quickshell_minimal"
type = "shell"
start = "qs -c minimal"
stop = "pkill -x qs"
start_check = "pgrep -x qs"
config_link = "~/.config/quickshell"
depends_on = ["hyprland"]
health_check = "qs -c minimal ipc show"

[[components]]
name = "waybar"
type = "bar"
start = "systemctl --user start waybar.service"
stop = "systemctl --user stop waybar.service"
reload = "systemctl --user reload waybar.service"
start_check = "systemctl --user is-active waybar.service"
config_link = "~/.config/waybar"
depends_on = ["hyprland"]

# waybar config di rice ini: ganti `niri/workspaces` → `hyprland/workspaces`
# (lihat §1.4 — waybar 0.10+ punya hyprland/workspaces module)

[[components]]
name = "mako"
type = "notifications"
start = "systemctl --user start mako.service"
stop = "systemctl --user stop mako.service"
config_link = "~/.config/mako"
```

**Catatan**: rice B butuh `~/.config/quickshell/minimal/` (Quickshell config baru, lebih ramping) yang **harus dibuat manual** — switcher tidak auto-generate Quickshell config. `rice add` cuma copy config aktif, user harus edit.

**Hasil validasi TOML**:
```
$ python3 -c 'import tomllib, sys; d = tomllib.loads(open(sys.argv[1]).read()); print("OK, keys:", list(d.keys()), "components:", len(d.get("components", [])))' /tmp/rice-validate/rice-b.toml
OK, keys: ['meta', 'theme', 'components'] components: 4
# exit 0
```


## 5. Keputusan Teknis (dengan trade-off)

### 5.1 Bahasa: Python 3.11+ dengan `tomllib`

| Opsi | Pro | Kontra |
|---|---|---|
| **Python 3.11+** (REKOMENDASI) | `tomllib` stdlib; sudah ada Python 3.14.7; familiar; cross-platform path handling; `subprocess` mature; `pathlib` bagus untuk atomic rename | startup lambat (~80ms); distribusi butuh venv atau script + shebang; **no `psutil`** (perlu install atau pakai `subprocess`+`pgrep` saja) |
| Bash | native di sistem; tidak butuh runtime; user hyprland pasti punya | manifest parsing di bash = reinvent TOML; error-prone; state management di `~/.cache/rice-switcher/state` bukan idiomatic; testing lebih susah |
| Rust | single binary; cepat | belajar curve; user belum eksplisit bilang mau Rust; over-engineered untuk tool CLI personal |
| Go | single binary; cepat | sama seperti Rust |

**Rekomendasi**: **Python 3.11+**. Alasan:
- `tomllib` built-in (no dependency untuk parsing manifest).
- `subprocess` + `shutil` cukup untuk pkill/systemctl/cp -a.
- Untuk cek proses hidup pakai `subprocess.run(["pgrep", ...])`, **tidak butuh `psutil`**.
- Distribusi: shebang `#!/usr/bin/env python3` + `pip install --user` opsional untuk future deps, atau zero-dep dengan stdlib saja.

**Default**: zero-dep (stdlib only). Kalau nanti butuh IPC yang lebih kaya, baru tambah `python-i3ipc` (sudah terinstall di pacman) atau `python-dbus`.

### 5.2 Process Management: langsung vs systemd user unit

| Opsi | Pro | Kontra |
|---|---|---|
| **Direct (subprocess)** (REKOMENDASI) | portable, no service file needed; bisa start/stop dengan `pkill` + `subprocess.Popen`; auto-cleanup saat parent mati; **konsisten dengan setup saat ini** (semua proses adalah Hyprland child atau direct exec, BUKAN systemd-managed) | perlu track PID manual; perlu `pgrep` pattern untuk stop_check |
| Systemd user unit | `systemctl --user` auto-manage; `Restart=on-failure`; logging | service file harus ditulis ke `~/.config/systemd/user/`; existing waybar/mako sudah punya template unit (disabled); Quickshell **tidak punya** unit (langsung dari hl.exec_cmd); `hypridle`, `hyprsunset`, `cliphist` watcher semua direct, bukan systemd |

**Rekomendasi**: **direct subprocess**. Alasan:
- 5 dari 7 proses aktif (qs, hypridle, wl-paste, geoclue) **bukan** systemd-managed.
- `hyprsunset` (pid 4722) PPID=1, tapi bukan dari `hyprsunset.service` (disabled) — spawned by Quickshell.
- `waybar` & `mako` punya unit template, tapi disabled; pakai direct `pkill waybar` lebih cepat daripada `systemctl stop`.
- Konsisten dengan pola `hyprland/execs.lua` (semua `hl.exec_cmd`).

**Default**: direct. `systemctl` adalah fallback kalau user pilih komponen "service" di manifest (untuk rice yang mau systemd-managed).

### 5.3 Snapshot & Rollback Strategy (revisi #3 — menjawab prompt §5)

**Apa yang disimpan dalam snapshot**:
- **Semua `config_link` target** (lihat §2.0) dari current rice. Untuk setiap `config_link` di manifest, snapshot direktori di belakangnya.
- Snapshot disimpan sebagai **self-contained directory tree** di `{state_home}/snapshot/<timestamp>/<config_link_basename>/`. Format: copy persis (symlink sebagai symlink, file sebagai file, permission/owner dimat).
- **TIDAK** menyimpan: state Quickshell (`.venv/`, `user/generated/`, `user/ai/chats/`), keyring, SSH keys, secrets, atau path di luar `config_link`.

**Kapan snapshot dibuat**:
- Sebelum perubahan apapun pada `config_link` (termasuk swap). Snapshot SELESAI dan tervalidasi (size, file count) sebelum swap dimulai.
- Setelah snapshot valid, **baru** swap dilakukan. Kalau snapshot gagal (disk full, permission denied), switch abort — tidak ada perubahan.
- Snapshot **temporary** (in-progress) disimpan ke `<state_home>/snapshot/.pending-<timestamp>/`; setelah swap sukses, di-rename ke `<state_home>/snapshot/<timestamp>/` dan masuk retensi.
- Snapshot **retensi** (keep last 5, lihat §5.4) disimpan di `<state_home>/snapshot/`.

**Bagaimana symlink & direktori ditangani**:
- Pakai `shutil.copytree(src, dst, symlinks=True)` atau `cp -a`. **JANGAN** `--dereference` (akan copy content symlink, hilang symlink semantics). **Diverifikasi** di `SnapshotAndRollbackTests::test_snapshot_preserves_symlinks_as_symlinks`.
- Untuk symlink yang dangling di live config (mis. `gtk-4.0` di setup), snapshot tetap copy symlink sebagai symlink (bukan error). **Diverifikasi** di `test_snapshot_round_trips_a_dangling_link`.
- File biasa + permission + metadata: `cp -a` / `shutil.copy2` preserves mode bits, mtime, owner (kalau root). Untuk snapshot root, owner root dimat; untuk user dir, owner = user. Disimpan as-is.

**Bagaimana snapshot dibedakan dari temporary**:
- `snapshot/.pending-<ts>/` → in-progress (tidak dihitung untuk retensi, dihapus kalau switch gagal).
- `snapshot/<ts>/` → committed retensi (dihitung untuk retensi).
- `snapshot/latest` → symlink ke yang terbaru (untuk `rice-switcher rollback`).

**Bagaimana pemulihan dilakukan jika switch gagal** (pada setiap tahap):

1. **Gagal di pre-swap validate**: tidak ada perubahan, tidak perlu rollback.
2. **Gagal di snapshot creation**: tidak ada perubahan, tidak perlu rollback.
3. **Gagal di stop komponen**: trigger_rollback → bring back stopped components (best-effort, lihat §7 R2). Symlink dan matugen BELUM disentuh.
4. **Gagal di swap symlink**: trigger_rollback → unlink new symlink, restore old symlink dari snapshot.
5. **Gagal di `hyprctl reload` / post-swap validate**: trigger_rollback → swap symlink back, restore from snapshot.
6. **Gagal di matugen**: matugen output ditulis ke symlink target (rice target dir). Symlink sudah di swap, jadi matugen output masuk rice target. **Tidak perlu rollback symlink**, tapi warna rice target mungkin tidak sesuai. User bisa re-trigger `matugen image` manual.
7. **Gagal di start komponen target**: trigger_rollback → swap symlink back + restart komponen rice lama (best-effort).
8. **Gagal di health check final**: sama seperti #7.

**Apa yang terjadi jika rollback itu sendiri gagal** (rollback of rollback):
- **Failure modes**:
  - Snapshot directory corrupt / missing → fallback ke `/TEST-RICE/<old_rice>/` direct copy (kalau rice lama masih ada).
  - Symlink unlink gagal (EBUSY, file busy) → retry 3x dengan sleep 1s; kalau masih gagal, exit 1 dengan pesan error eksplisit.
  - Component restart gagal (process won't start) → `notify-send "rice-switcher: rollback FAILED, desktop in unknown state. Check logs."` (audio + visual) dan exit 1.
  - Lock file corrupt → `flock` returns error; user harus `rm /path/to/switch.lock` manual.
- **Status akhir** di state.json: `{ "last_switch": "FAILED", "previous_rice": "<name>" }` (BUKAN `current_rice`).
- **Audit log**: setiap rollback attempt dicatat dengan exit code.

**Batasan transaksi penuh** (prompt §5 eksplisit):
- **Proses yang sudah dihentikan** (to_stop) **TIDAK BISA di-restart** kalau binary hilang atau user membunuh parent shell. Rollback mencoba best-effort; kalau gagal, desktop ada dalam **partial state** yang hanya bisa diperbaiki manual.
- **Hook side-effects** (`hooks.pre`, `hooks.post`) **TIDAK reversible**. Mis. kalau hook `pre` kill proses X, rollback tidak menghidupkan X kembali. Dokumentasi: hook harus idempotent dan reversible.
- **Matugen output** adalah **side-effect** yang tidak bisa di-undo dengan rollback. Snapshot hanya menyimpan config; warna yang di-render setelah swap ada di luar snapshot.
- **Deadlock** antara snapshotted processes (mis. Quickshell IPC ke service yang sudah di-stop) — recover manual.
- **Transaction penuh TIDAK dimungkinkan** untuk komponen yang side-effectnya di luar filesystem. Kami accept ini. Dokumentasi user harus jelas tentang batasan ini.

**Snapshot itu sendiri TIDAK menyimpan data sensitif**:
- `~/.config/quickshell/ii/user/ai/chats/` TIDAK di-include di `config_link` (lihat §2.0). Snapshot tidak menyentuh.
- `~/.config/illogical-impulse/config.json` mengandung `userAgent` dll. (low risk) — tetap di-snapshot. Risk assessment: §8.
- Token, API keys, .env: **TIDAK** pernah masuk `config_link`. Snapshot aman.
- Audit di §7 R7: "Jangan menyimpan token atau data rahasia secara tidak sengaja".

**Diverifikasi** semua di `SnapshotAndRollbackTests` (4 tests).

### 5.4 Lock & Concurrency

**Default**: `flock` di `state.lock` (file di `~/.local/state/rice-switcher/`). Abort kalau double-switch dalam 5 detik. 5 detik = cukup untuk switch normal, tapi tidak mengunci kalau user cancel.

### 5.5 State File

`state.json` di `~/.local/state/rice-switcher/state.json`:
```json
{
  "current_rice": "illogical-impulse",
  "last_switch": "2026-10-16T22:30:00+07:00",
  "active_components": {"hyprland": true, "quickshell_ii": true, ...}
}
```

Auto-detect `current_rice` via heuristic: cari rice yang config_link targetnya cocok dengan current symlink (atau realpath). Fallback: scan processes dan cocok dengan start pattern.

---

## 6. Roadmap Bertahap (revisi #2)

### 6.1 Slice 1: switch + symlink + start/stop + **dry-run + snapshot** (MVP)

- `rice-switcher init` (scaffold dirs, state.json)
- `rice-switcher add <name>` (copy current config ke `rice/<name>/`)
- `rice-switcher list` / `status` / `current`
- `rice-switcher switch <name>` — full flow, dengan:
  - **Pre-swap validate**: rice.toml schema + binary presence + `hyprland --verify-config --config <target>`
  - **Snapshot otomatis** sebelum perubahan
  - **`--dry-run`**: print plan, no changes
  - **Lock file** (`flock`) untuk anti double-switch
  - **Managed set** auto-computed
  - **Rollback otomatis** kalau health check gagal
- `rice-switcher snapshot` (manual)
- `rice-switcher rollback`
- Validasi: rice.toml schema + binary + config_link (no nesting, parent `hypr`/`quickshell`/etc)
- Test hermetic dengan stub (`RICE_TEST=1`)

**Done when**: bisa switch 2 rice (Quickshell + Waybar) bolak-balik dalam stub mode tanpa menyentuh desktop asli. Real run opsional tapi harus gagal-gracefully.

### 6.2 Slice 2: UX minimum

- `rice-switcher validate <name>` (comprehensive: binary, path, matugen template syntax, hyprland --verify-config)
- `rice-switcher status` (real-time table)
- Better error messages + audit log
- `rice-switcher keybind-install` (append ke `~/.config/hypr/custom/keybinds.lua`)

**Done when**: validate mendeteksi semua masalah umum, status real-time, keybind installed.

### 6.3 Slice 3: random + menu + apply

- `rice-switcher random` (pick random + switch)
- `rice-switcher menu` (TUI picker — `fuzzel` atau `rofi -dmenu` sesuai rice)
- `rice-switcher apply` (restore rice terakhir — dipanggil dari `hyprland/execs.lua` di `hl.on("hyprland.start")`)
- Lifecycle migration: hapus baris `hl.exec_cmd` individual, ganti `rice-switcher apply` (lihat §2d)
- Configurable post-switch notification (`notify-send`)

**Done when**: dari terminal/keybind, user bisa switch rice dalam ≤3 detik. Setelah reboot, rice terakhir auto-restore.

### 6.4 Slice 4: import + diff + edit

- `rice-switcher import <path>` (read-only, see §2h)
- `rice-switcher diff <a> <b>` (config diff)
- `rice-switcher edit <name>` (open rice.toml di `$EDITOR`)
- Integration dengan `systemd --user` (opsional, opt-in per komponen)

**Done when**: tool terasa lengkap untuk daily use, README jelas.

---

## 7. Risiko & Edge Case

| # | Risiko | Cara test |
|---|---|---|
| R1 | Komponen gagal start (mis. `qs -c minimal` error: config not found) → rice half-applied | Tambah rice B tanpa `~/.config/quickshell/minimal/`, switch, expect rollback + error message |
| R2 | Komponen tidak mau mati (`pkill` tidak efek) | Stub `pkill` jadi no-op, switch, expect timeout → fallback `pkill -9` |
| R3 | Dua rice share komponen (mis. hypridle di A dan B) | Definisikan komponen di kedua rice, switch A→B, expect hypridle **tidak** restart (di-skip karena sudah running) |
| R4 | Switch dobel (user tekan keybind 2x cepat) | Lock file, tekan 2x, expect yang kedua abort |
| R5 | Symlink rusak (rice source dihapus, target dangling) | `rm -rf rice/A/quickshell`, switch, expect validate gagal sebelum switch, atau health_check detect |
| R6 | matugen gagal mid-render (template syntax error) | Edit template matugen salah, switch wallpaper, expect rollback |
| R7 | hyprland config parse error (`hyprctl reload` gagal) | Edit `hyprland.lua` dengan syntax error di config_link, switch, expect rollback via `hyprland --verify-config` |
| R8 | `hyprsunset` (pid 4722) PPID=1, bukan Hyprland child — orphan | `pkill hyprsunset`, check apakah start.sh (di Quickshell Hyprsunset.qml:87) re-spawn |
| R9 | `gnome-keyring-daemon` (pid 2502) — **JANGAN** di-stop saat rice switch (auth dependency) | **Solved**: tidak masuk managed set. Tetap di-start dari `hyprland/execs.lua`. Switcher tidak pernah touch. |
| R10 | Clipboard history (`wl-paste` watcher) — **JANGAN** di-stop (clipboard state hilang) | **Solved**: `cliphist_text` & `cliphist_image` di-exclude dari managed set |
| R11 | `~/.config/gtk-4.0` adalah **dangling symlink** (target dir tidak ada). Matugen output_path adalah `~/.config/gtk-4.0/gtk.css` (file, bukan dir). Matugen akan gagal mkdir di target | `mkdir -p` target sebelum matugen, atau update `nwg-look/config:export-gtk4-symlinks=false` dan biarkan GTK4 pakai default. Lihat §2f Aturan 4 & §9.3 |
| R12 | Quickshell `ii` config (931 files, 4.3M after trim, 7.7M full) — symlink ke 931 files. Switch = `mv`/`ln -s` ratusan inode. Cepat tapi race window kecil | **Solved**: parent swap `~/.config/quickshell` (1 syscall) |
| R13 | Waybar `left.json` punya module `niri/workspaces` (stale, niri config tidak ada) | Saat rice switch, waybar mungkin warn/error. Solusi: rice B (atau rice example) punya waybar config bersih dengan `hyprland/workspaces` (waybar 0.10+ support). Lihat §1.4 |
| R14 | `awww-daemon` / `swww` / `mpvpaper` TIDAK terinstall, tapi matugen wallpaper path ada. Rice yang set wallpaper tetap harus punya fallback | `wallpaper` field opsional, kalau tidak ada, skip matugen. Fallback ke QS image rendering |
| R15 | `easyeffects` NOT installed, tapi `execs.lua:16` referensinya. Quickshell silent fail | `optional = true` di manifest; switcher skip kalau binary tidak ada |
| R16 | Cursor theme inkonsisten saat ini (hyprland vs GTK). Rice A & B harus eksplisit | **Solved**: `[theme].cursor_name` + `cursor_size` di manifest. Switcher apply ke `hyprctl setcursor` + `gsettings` supaya konsisten. Inkonsistensi `Bibata-Modern-Classic 24` vs `Bibata-Modern-Ice 20` di setup adalah bug existing, didokumentasikan di §2b & §8 |
| R17 | `gnome-keyring-daemon --start` di `execs.lua:10` start setiap Hyprland start. Tidak conflict dengan switch, tapi **jika** rice switch men-stop hypridle (yang lock_cmd=quickshell), maka keyring tetap hidup, hyprlock tetap bisa baca keyring | Verifikasi: matikan hypridle, kunci via `loginctl lock-session`, unlock dengan password (bukan keyring unlock) |
| R18 | `~/.config/illogical-impulse/config.json:background.wallpaperPath` hardcoded. Matugen output juga tulis ke `~/.local/state/quickshell/user/generated/wallpaper/path.txt`. Rice yang override wallpaper harus update keduanya | `rice add` menyertakan file ini; switch + matugen akan auto-update path.txt |
| R19 | `~/.local/state/quickshell/.venv` (Python 3.12.15) **tidak** di-swap. Quickshell venv ini depend on Python 3.12. Jika Quickshell di-update ke versi yang butuh Python 3.13, venv invalid. **Di luar scope** rice switch | Dokumentasi: rice switch tidak manage Python venv. Rebuild manual kalau Quickshell update |
| R20 | `pkill -f "qs -c ii"` bisa match `pkill -f "qs -c ii"` itu sendiri / shell wrapper | **Solved** (§2g): stop default = `kill <pid>` setelah scan `/proc/*/cmdline` exact-match basename. `pkill -f` di-ban di manifest validator |
| R21 | `~/.config/gtk-3.0/gtk.css` adalah matugen output (di-overwrite tiap wallpaper change). Rice B yang override GTK theme harus `gsettings set` di post-hook | **Solved**: `[theme].gtk_theme` field → switcher run `gsettings set org.gnome.desktop.interface gtk-theme X` sebelum swap. Lihat §2b |
| R22 | HyDE leftovers (`wallbash/`, `niri.service.wants/`, waybar `niri/workspaces`). Tidak di-swap, tapi config referenced. **TIDAK** boleh di-include di fixture rice example | Saat `rice add`, hanya copy `config_link` paths yang didefinisikan di manifest, bukan scan semua `~/.config/`. Lihat §1.4 & §2f |
| R23 | Rice.toml `start`/`stop`/`hooks` menjalankan shell command arbitrary. Rice dari sumber luar bisa berbahaya | **Solved** (§2h): `import` read-only, `switch` print semua command + confirm prompt untuk rice baru, audit log |
| R24 | Matugen menulis file lewat symlink ke dalam folder rice → repo kotor | **Solved** (§2b): matugen output paths di-`.gitignore` |
| R25 | Hyprland config (Lua) salah syntax → switch bikin desktop crash | **Solved** (§2i + §3.2): pre-swap `hyprland --verify-config --config <target>`; post-swap sebelum reload. `--verify-config` support path lain (diverifikasi) |
| R26 | Switch dobel (race) | **Solved**: `flock` lockfile, abort kalau held (Slice 1) |
| R27 | `hyprland.lua` tidak ikut di-swap kalau hanya `hyprland/` subdir di `config_link` | **Solved** (§2f Aturan 3): validate cek `hyprland.lua` ada di swap path, reject kalau tidak |

---

## 8. Temuan Sensitif

> **Bagian ini sengaja tidak dipublikasikan** karena berisi temuan sensitif spesifik mesin penulis (lokasi kunci/secret, dan sejenisnya). Lihat `PLAN.local.md`.

## Keputusan Desain (dari §9.0)


| # | Pertanyaan | Keputusan revisi #3 |
|---|---|---|
| 1 | Compositor handling | `compositor` type dijamin **tidak pernah** masuk `to_stop` atau `to_start`. Diverifikasi di test 10. |
| 2 | Process identity | `executable` + `argv_match` per komponen. `pkill -f` dilarang. `find_pids_by_identity` via `/proc/*/cmdline` scan. Diverifikasi di test 9. |
| 3 | config_link conflict | 7 aturan termasuk symlink-equivalent detection, escape prevention (Aturan 7), `realpath` hanya untuk deteksi bukan normalisasi. Diverifikasi di test 3-8. |
| 4 | Snapshot model | Self-contained dir, `.pending-` temp, `latest` symlink, retensi 5, tidak depend rice dir. Diverifikasi di test 21-24. |
| 5 | Rollback | Batasan transaksi penuh didokumentasikan eksplisit (§5.3). Failure modes → exit 1 + audit. |
| 6 | Hyprland verify-config | `--verify-config --config <path>` **hanya** cek syntax, bukan referenced path. Batasan Slice 1. Diverifikasi di test 16-18. |
| 7 | Matugen ordering | Direct `matugen image`, bukan via Quickshell IPC. Outputs masuk symlink target rice, di-`.gitignore`. |
| 8 | `--dry-run` | Di Slice 1 (bukan Slice 2). Diverifikasi di test 19-20. |
| 9 | Lock | `flock(2)` di test mode, `LOCK_EX \| LOCK_NB` kedua. Diverifikasi di test 26-27. |
| 10 | HyDE leftovers | Biarkan, exclude dari rice add. |
| 11 | Hyprland swap scope | Parent `~/.config/hypr` (atomic). `hyprland.lua` ikut. |
| 12 | Custom folder layering | Full snapshot per rice. |
| 13 | Bahasa Python 3.11+ | ✓ (zero-dep stdlib) |
| 14 | Process management direct | ✓ |
| 15 | State location `~/.local/state/rice-switcher/` | ✓ |
| 16 | Logging path | ✓ (`~/.local/state/rice-switcher/logs/`) |
| 17 | Snapshot retention | ✓ (5 retensi; snapshot sementara `.pending-<ts>/` tetap sampai transaksi selesai) |
| 18 | Lock timeout | ✓ (5 detik; abort tanpa mengubah desktop) |
| 19 | License | ✓ (MIT) |
| 20 | Validasi referenced path di Hyprland config | ✓ (basic Slice 1: parse `require("...")` sederhana di file target; tidak menggantikan `--verify-config`, hanya menambah) |
| 21 | Script hyprlock hilang | ✓ (prasyarat per rice; tidak mengubah setup asli di Slice 1) |
| 22 | Symlink `gtk-4.0` dangling | ✓ (diverifikasi read-only; tidak diperbaiki; dokumentasi & tidak menjalankan matugen pada desktop asli di Slice 1) |
