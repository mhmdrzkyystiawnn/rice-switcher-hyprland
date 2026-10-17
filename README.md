# rice-switcher-hyprland

> Switch between Hyprland "rices" by swapping config symlinks — safely, with snapshots, rollback, and a dry-run-by-default workflow.

![status](https://img.shields.io/badge/status-slices%201--4%20implemented-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

Hyprland has no built-in way to swap between whole desktop setups. If you keep several rices (for example a Quickshell-based one and a minimal Waybar one), switching by hand means swapping a pile of config directories and hoping nothing breaks.

**rice-switcher** records each rice in a `rice.toml` manifest and swaps the `config_link` symlinks for you, with a snapshot taken before every change so you can roll back.

> **Project status:** the CLI implements Slices 1–4 (see [Roadmap](#roadmap)). Everything documented below describes what the code does *today*. Features that are designed but **not** implemented are collected in [Not yet implemented](#not-yet-implemented); do not assume they exist.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Commands](#commands)
- [The `rice.toml` manifest](#the-ricetoml-manifest)
- [Directory layout](#directory-layout)
- [Safety model](#safety-model)
- [Security](#security)
- [Configuration & environment variables](#configuration--environment-variables)
- [Matugen and generated files](#matugen-and-generated-files)
- [Testing](#testing)
- [Not yet implemented](#not-yet-implemented)
- [Roadmap](#roadmap)
- [Known limitations](#known-limitations)
- [License](#license)

---

## Features

- **Symlink-based config swapping:** each `config_link` in the target rice is symlinked from its live path (e.g. `~/.config/hypr`) to the rice's copy. The old entry is preserved as `<path>.bak-<timestamp>`.
- **Snapshots & rollback:** the live `config_link` targets of the current rice are snapshotted before a swap. `rollback` restores them and re-syncs `state.json`.
- **Dry-run by default:** `switch` and `import` change nothing unless you pass `--no-dry-run` / `--apply`.
- **Pre-flight validation:** `switch` validates the target manifest first (schema, `config_link` conflicts and escape checks, missing `start`). Errors abort the switch; warnings are printed. `validate` runs a deeper check (binaries, Hyprland config, matugen templates).
- **Identity-aware process probing:** process identity is `(executable, argv_match)`, so `qs -c ii` and `qs -c minimal` are distinguished. Used by `status` to report running PIDs; see [Not yet implemented](#not-yet-implemented) for what this does *not* do yet.
- **Lock file:** an `flock`-based lock with a 5-second timeout prevents concurrent switches.
- **Audit log:** every real action appends one JSON Lines event, with sensitive values redacted.
- **Zero dependencies:** Python 3.11+ standard library only.

---

## How it works

### Core concepts

| Concept | Meaning |
|---|---|
| **Rice** | A directory under `<RICE_HOME>/<name>/` containing a `rice.toml` manifest plus that setup's config files. |
| **Component** | One thing the rice describes: `hyprland`, a shell, a bar, a notification daemon, etc. Declared as `[[components]]` in the manifest. |
| **`config_link`** | The live path (e.g. `~/.config/hypr`) that gets symlinked to the rice's copy of the config. |
| **Managed set** | The union of all components across all your rices, used to compute the switch plan. |
| **Process identity** | `(executable, argv_match)`, used to decide whether two components refer to the same process. |

### What `switch` does today

1. **Locate** the target rice; abort if unknown.
2. **Validate** the target manifest (`validate_manifest`). `config_link` conflicts / escape and a missing `start` for a non-optional component are errors; missing binaries and other issues are warnings.
3. **No-op check** — if the target is already the current rice, print a no-op plan and stop.
4. **Compute the plan** — `to_stop`, `to_start`, `to_reload`, `to_swap`, `to_keep` — from the managed set and the current symlink state.
5. **Dry-run** (the default) prints the plan and returns without touching anything.
6. **Acquire the lock** (5-second timeout).
7. **Snapshot** the current rice's `config_link` targets into `snapshot/.pending-<ts>/` (recording the current rice name in `.captured.txt`).
8. **Swap each `config_link`** to the target rice's copy; the previous entry is renamed to `<path>.bak-<ts>`.
9. **Commit** the pending snapshot, then prune old snapshots (keep 5).
10. **Update `state.json`** (`current_rice` + trust cache) and append an audit event.
11. If anything fails after step 7, the `config_link`s are restored from the pending snapshot, the pending snapshot is discarded, the trust entry is marked `rolled_back`, and the error is returned.

The compositor (`type = "compositor"`) is never stopped or started — `switch` never sends signals to any process.

### What `switch` does **not** do

`switch` only swaps symlinks, snapshots, and updates state. It does **not** (yet):

- **start or stop components.** `to_stop` / `to_start` are computed and printed, but no process is signalled. Autostart remains the job of the `hl.exec_cmd` lines in your `hyprland/execs.lua`.
- **apply `[theme]`** (cursor / GTK / icon).
- **run `[hooks]`** pre/post.
- **run matugen** or regenerate colors.
- **reload Hyprland** (`hyprctl reload`).

See [Not yet implemented](#not-yet-implemented).

---

## Requirements

**Required**

- Python **3.11+** (uses `tomllib`). No runtime dependencies.

**Used only by specific commands**

- `hyprctl` — needed by `apply --require-session` to probe for a live Hyprland session.
- `hyprland` — needed by `validate` to run `hyprland --verify-config --config <path>`.
- An editor (`$EDITOR` / `$VISUAL` / `vi`) — needed by `edit`.
- `fuzzel` or `rofi` — needed by `menu` (either one).

Nothing else is executed by the tool. Binaries named in a component's `start` are only *looked up on `PATH`* by validation (a warning if missing) — they are not run.

---

## Installation

Run directly from the repository:

```bash
git clone <repo-url> ~/projects/rice-switcher-hyprland
cd ~/projects/rice-switcher-hyprland

# Run as a module
python3 -m rice_switcher --help
```

Or install the `rice-switcher` console script defined in `pyproject.toml`:

```bash
python3 -m pip install --user .        # or: pipx install .
# editable, for development:
python3 -m pip install --user -e .
```

> **Back up first.** Before using the tool on a real session, make a full backup of your `~/.config` (for example `rsync -a ~/.config/ ~/rice-backup/$(date +%Y%m%d_%H%M%S)/`). Never include `~/.ssh`, `~/.gnupg` or keyrings in backups you plan to share.

---

## Quick start

```bash
# 1. Create the state directories and state.json
rice-switcher init

# 2. Scaffold a new rice (writes a template rice.toml; nothing is copied yet)
rice-switcher add my-first-rice

# 3. Edit the generated manifest
$EDITOR ~/rice/my-first-rice/rice.toml

# 4. Add a second rice the same way, then check what you have
rice-switcher list
rice-switcher current

# 5. Preview a switch (no changes are made)
rice-switcher switch waybar-minimal --dry-run

# 6. Do it for real
rice-switcher switch waybar-minimal --no-dry-run
```

`switch` is a **dry-run by default**; you must pass `--no-dry-run` to apply changes. Rices that have never been switched on this machine, or whose manifest hash has changed, ask for an explicit `yes` confirmation after printing the plan.

---

## Commands

All of the following are implemented.

| Command | Description |
|---|---|
| `rice-switcher init` | Create the rice directory, `state.json`, the snapshot directory and the log directory. Idempotent. |
| `rice-switcher add <name>` | Scaffold `<RICE_HOME>/<name>/rice.toml` from a template. Refuses to overwrite an existing manifest; does **not** copy your current config. |
| `rice-switcher list` | List available rices (directories that contain a `rice.toml`). |
| `rice-switcher current` | Print the active rice from `state.json` (or a "never switched" message). |
| `rice-switcher status` | Table per rice: symlink state, symlink owner, probed component PIDs, drift, last-switch time. |
| `rice-switcher validate <name>` | Deep validation: schema, binaries, `config_link`, Hyprland config, matugen templates. Exit 1 if errors. |
| `rice-switcher switch <name> [--dry-run] [--no-dry-run]` | Switch to a rice. See [How it works](#what-switch-does-today). Default dry-run. |
| `rice-switcher snapshot` | Take a manual snapshot of the current rice's `config_link` targets. |
| `rice-switcher rollback [ts]` | Restore a snapshot (latest if omitted) and re-sync `state.json`. |
| `rice-switcher apply [--require-session]` | Re-apply the rice recorded in `state.json`. Intended for `hl.on("hyprland.start", ...)`. Runs a real swap without the confirmation prompt. |
| `rice-switcher import <path> [--name N] [--apply]` | Import an existing config directory as a new rice. Default dry-run; `--apply` copies. Read-only with respect to your live desktop. |
| `rice-switcher diff <a> <b>` | Unified diff of two rices' `rice.toml`. Exit 0 same, 1 different, 2 error. |
| `rice-switcher edit <name> [--editor CMD]` | Open `rice.toml` in `$EDITOR` (falls back to `$VISUAL`, then `vi`). |
| `rice-switcher menu [--picker N] [--dry-run] [--require-session]` | Pick a rice with `fuzzel`/`rofi` and switch to it. |
| `rice-switcher keybind-install [--path P] [--command C] [--dry-run] [--allow-any-path]` | Append a `SUPER+SHIFT+R` bind to `~/.config/hypr/custom/keybinds.lua`. |

`switch` accepts `--dry-run` (default) and `--no-dry-run`. `import` accepts `--dry-run` (default) and `--apply`.

---

## The `rice.toml` manifest

Each rice lives in `<RICE_HOME>/<name>/rice.toml`. Scalars come first, then tables, then `[[components]]`.

```toml
[meta]
name = "illogical-impulse"          # required, unique
description = "Quickshell ii with matugen theming"
author = "your-name"

config_dir = "./"                    # relative to rice.toml
wallpaper = "./wallpapers/default.png"

[theme]                              # parsed, but NOT applied yet
cursor_name = "Bibata-Modern-Classic"
cursor_size = 24
gtk_theme = "Wallbash-Gtk"
icon_theme = "Tela-circle-dracula"
font_main = "Google Sans Flex"
font_mono = "JetBrains Mono Nerd Font"
# apply = "~/rice/illogical-impulse/scripts/apply-theme.sh"   # optional custom command

[hooks]                              # parsed, but NOT run yet
pre = []
post = []

[[components]]
name = "hyprland"
type = "compositor"
start = "true"                       # only checked for "is the binary on PATH?"
stop = "true"
reload = "hyprctl reload"
config_link = "~/.config/hypr"       # swap the PARENT directory

[[components]]
name = "quickshell_ii"
type = "shell"
executable = "qs"
argv_match = ["-c", "ii"]
start = "qs -c ii"
stop = "pkill -x qs"
start_check = "pgrep -x qs"
health_check = "qs -c ii ipc show"
config_link = "~/.config/quickshell"
depends_on = ["hyprland"]

[[components]]
name = "mako"
type = "notifications"
start = "systemctl --user start mako.service"
stop = "systemctl --user stop mako.service"
reload = "makoctl reload"
config_link = "~/.config/mako"

[[components]]
name = "easyeffects"
type = "system"
start = "easyeffects --hide-window --service-mode"
stop = "pkill -x easyeffects"
optional = true                      # skipped by validation if `start` has no binary on PATH
```

`pkill -f` / `pkill --full` is considered dangerous because it matches the full command line (including the switcher itself). A helper (`looks_like_dangerous_pkill`) exists for this and is unit-tested, but it is **not yet wired into manifest validation**, so such patterns are not rejected today.

### Component fields

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique identifier within the rice; used by `depends_on`. |
| `type` | yes | `compositor`, `shell`, `bar`, `notifications`, `locker`, `wallpaper`, `terminal`, `launcher`, `theme_generator`, `color_daemon`, `clipboard` or `system`. |
| `start` | yes (unless `optional`) | Command to start the component. `true` is a no-op. Validation only checks the first word against `PATH`. |
| `stop` | no | Command to stop the component. Defaults to `true`. |
| `executable` | no | Executable basename for process identity (defaults to `name`). |
| `argv_match` | no | Tokens that must appear in the process argv, to tell apart e.g. `qs -c ii` from `qs -c minimal`. |
| `reload` | no | Live-reload command (recorded in the manifest; not executed yet). |
| `config_link` | no | Path symlinked to the rice's copy. Only components that set it are swapped. |
| `depends_on` | no | Components that must start first (recording only). |
| `start_check` / `stop_check` | no | Verify a process is up / down. Defaults to a `/proc` identity scan. |
| `health_check` | no | Command that exits 0 when healthy (recording only). |
| `optional` | no | Exempt from the "missing binary" and "missing start" checks. |
| `timeout` | no | Non-negative integer (default 10; recording only). |
| `match` | no | Free-form match string (recording only). |

### `[theme]` fields

`cursor_name`, `cursor_size`, `gtk_theme`, `icon_theme`, `font_main`, `font_mono`, `font_size`, `apply`. All optional. They are parsed and stored on the manifest, **but `switch` does not apply them yet**.

### Rules for `config_link`

Enforced by `validate_manifest` (which `switch` runs before doing anything):

1. Two `config_link`s in one rice must not be **identical** or **nested** (parent/child). This is an **error**.
2. Declaring `hypridle.conf` or `hyprlock.conf` while an ancestor is already managed produces a **warning** (avoid the double-swap).
3. `config_link` must resolve under `config_home`, `state_home` or `rice_home`. Anything else (`/etc/passwd`, another user's home) is an **error**.
4. Paths are normalized with `abspath` + `expanduser`, **never** `realpath`, so dangling symlinks are preserved and compared literally.
5. A `config_link` that doesn't exist yet is allowed; `switch` creates it.
6. Swapping `~/.config/hypr` (the parent) covers `hypridle.conf` / `hyprlock.conf` / `hyprland.lua`; don't register those individually.

### What never goes in a manifest

Components that must survive every rice switch are intentionally **not** declared, so they are never part of the managed set: `gnome-keyring-daemon`, `geoclue`, `ydotoold`, and the `wl-paste` + `cliphist` clipboard watchers. Keep starting them from `hyprland/execs.lua` as before.

---

## Directory layout

```
rice-switcher-hyprland/
├── rice_switcher/          # Python package (stdlib only)
│   ├── cli.py              # argparse + command dispatch
│   ├── manifest.py         # TOML loading + dataclasses
│   ├── validate.py         # schema, binaries, config_link rules
│   ├── managed_set.py      # managed set + identity-aware diff
│   ├── process.py          # /proc scan, identity matching (read-only)
│   ├── snapshot.py         # snapshot + rollback + symlink swap
│   ├── switch.py           # switch orchestrator
│   ├── apply.py            # `apply` (restore from state.json)
│   ├── menu.py             # `menu` picker
│   ├── keybind.py          # `keybind-install`
│   ├── import_diff_edit.py # `import` / `diff` / `edit`
│   ├── lock.py             # flock with timeout
│   ├── state.py            # state.json
│   ├── paths.py            # path roots + overrides
│   └── log.py              # text log + JSON Lines audit log
├── fixtures/               # sanitized sample configs (reference only)
├── tests/
│   ├── design/test_design.py
│   ├── test_slice1.py … test_slice4.py
│   ├── test_slice3_apply_menu.py
│   ├── test_audit.py
│   └── test_final_verify.py
├── PLAN.md                 # full design document
├── LICENSE
└── README.md
```

Runtime state lives in `<RICE_STATE_HOME>/rice-switcher/`: `state.json`, `switch.lock`, `snapshot/`, `logs/switcher.log`, `logs/audit.log`.

---

## Safety model

**Snapshots.** Before any swap, every `config_link` target of the *current* rice is copied into `snapshot/.pending-<ts>/` (symlinks preserved as symlinks, dangling links included; `shutil.copytree(symlinks=True)`, never dereferenced). On success it is renamed to `snapshot/<ts>/` and `snapshot/latest` is updated. The 5 most recent snapshots are kept. Each snapshot stores a `.captured.txt` listing the captured paths and the rice name.

**Rollback.** `switch` rolls back automatically if a step fails after the snapshot is taken: the `config_link`s are restored from the pending snapshot, the pending snapshot is discarded, and `state.json` marks the rice `rolled_back`. `rice-switcher rollback [ts]` restores a committed snapshot manually and re-syncs `current_rice` to the rice recorded in the snapshot. Rollback restores **files and symlinks only** — it does not restart processes.

**Hyprland config validation.** Only the `validate` command runs `hyprland --verify-config --config <path>` (a non-zero result or timeout is an error; a missing `hyprland` binary is a warning). `switch` itself only does a basic `require("...")` / `source = ...` referenced-path check on `hyprland.lua` (warnings, not errors). `--verify-config` checks syntax only.

**Locking.** `flock(2)` on `switch.lock` with a 5-second timeout. A second switch while one is running aborts without changing anything.

---

## Security

A `rice.toml` may one day run shell commands on your machine. Today the tool **does not execute** `start`, `stop`, `reload`, `hooks.*` or `theme.apply` — it only validates them and probes process identity. Treat that as a limitation, not a guarantee, and still treat rices from other people like any script you download.

- `import` is read-only: it parses the manifest and copies files, and never executes anything from the source manifest.
- The first `switch` to a rice prints the plan and requires typing `yes` (unless the rice is already trusted and its manifest hash is unchanged).
- Approval is tied to the manifest's SHA-256. If the manifest changes, you are asked again.
- The tool's own subprocess calls (editor, picker, `hyprland --verify-config`) use argv lists, never `eval`. A `looks_like_dangerous_pkill` helper flags `pkill -f` / `pkill --full` (too broad, and it matches the switcher itself), but it is not yet called by validation.
- Actions are recorded in a JSON Lines audit log (`logs/audit.log`). Sensitive keys (`api_key`, `token`, `secret`, …) and secret-shaped values are redacted before writing. There is no auto-delete; old `audit-*.log` archives are gzipped once older than `RICE_AUDIT_ROTATE_DAYS` (default 30).
- Because commands are not executed yet, the audit log records *what the tool did* (switch, rollback, import, edit, keybind-install, …), not command exit codes.

**Before publishing your own rices**, check for personal data: `grep -r "/home/<you>" rice/`, redact private model/endpoint descriptions in app configs, and never commit browser profiles, keyrings, SSH/GPG keys or `.env` files. The shipped `.gitignore` already excludes `*.env`, `id_*`, `.ssh/` and `.gnupg/`.

---

## Configuration & environment variables

| Variable | Purpose |
|---|---|
| `RICE_HOME` | Root of the rice directory (default `~/rice`). |
| `RICE_CONFIG_HOME` | Override `XDG_CONFIG_HOME` for symlink targets (default `~/.config`). |
| `RICE_STATE_HOME` | Override `XDG_STATE_HOME` for state/snapshot/lock/logs (default `~/.local/state`). |
| `RICE_LOG_FILE` | Custom text log path (default `<RICE_STATE_HOME>/rice-switcher/logs/switcher.log`). |
| `RICE_DRY_RUN=1` | Force dry-run on the switch path (`switch`, `apply`, `menu`). |
| `RICE_AUDIT_ROTATE_DAYS` | Gzip audit archives older than this many days (default `30`). |
| `RICE_TEST=1` | **Not implemented** — see [Not yet implemented](#not-yet-implemented). |

The same path overrides can be set per rice in the `[run]` table of `rice.toml` (`rice_home`, `config_home`, `state_home`).

---

## Matugen and generated files

The tool does **not** run matugen. It only *validates* a rice's matugen config (TOML well-formedness and template `input_path` / `output_path` checks) during `rice-switcher validate`.

If you run matugen yourself (or via your shell), its output can land inside your rice directories through the symlinked config paths. Add those generated files to `.gitignore` so they are not committed:

```gitignore
# Matugen-generated
**/hyprland/colors.lua
**/hyprlock/colors.conf
**/fuzzel/fuzzel_theme.ini
**/gtk-3.0/gtk.css
**/illogical-impulse/config.json
**/user/generated/colors.json
**/user/generated/wallpaper/path.txt
```

---

## Testing

The suite uses `unittest` and runs hermetically (everything under temporary directories; it does not start or stop processes, reload Hyprland, or write to `~/.config`).

```bash
# one file
python3 tests/test_slice1.py

# everything
for f in tests/test_*.py tests/design/test_*.py; do python3 "$f"; done
```

Note: `python3 -m unittest discover -s tests` does **not** work because `tests/` is not an importable package (no `__init__.py`); use one of the commands above.

The suite covers managed-set computation, `config_link` conflict detection, process identity, the switch plan (including "compositor never stopped" and "shared component not restarted"), dry-run purity, snapshot and rollback with symlinks, locking, the JSON Lines audit log, and the Slice 2–4 commands (`validate`, `status`, `apply`, `menu`, `keybind-install`, `import`, `diff`, `edit`), plus a dedicated design test file.

---

## Not yet implemented

These are designed in `PLAN.md` but not implemented in the code:

- **Start/stop of components.** `switch` computes a `to_stop` / `to_start` plan but never signals a process.
- **Applying `[theme]`** (cursor / GTK / icon / fonts).
- **Running `[hooks]`** pre/post.
- **Running matugen** to regenerate colors.
- **Reloading Hyprland** (`hyprctl reload` / `hyprctl configerrors`).
- **Automatic rollback of processes** (only files/symlinks are rolled back).
- **`random`** and **`remove`** subcommands.
- **Hermetic test mode** (`RICE_TEST=1`): no code reads this variable; tests use their own temporary roots instead.

---

## Roadmap

| Slice | Scope | Status |
|---|---|---|
| **1. Core** | `init`, `add`, `list`, `current`, `status`, `switch` (+ `--dry-run`), `snapshot`, `rollback`; validation; managed set; locking | Implemented |
| **2. UX** | `validate`, richer `status`, audit log + rotation, `keybind-install` | Implemented |
| **3. Automation** | `menu`, `apply` | Implemented |
| **3. Automation (rest)** | `random`, autostart migration, notifications, real process start/stop | Not implemented |
| **4. Sharing** | `import`, `diff`, `edit` | Implemented |
| **4. Sharing (rest)** | `remove`, optional systemd integration | Not implemented |

### Autostart (Slice 3)

`apply` exists, so you can wire it into your Hyprland config by replacing the individual `hl.exec_cmd(...)` lines for managed components in `hyprland/execs.lua` with a single call inside the start event:

```lua
hl.on("hyprland.start", function ()
    hl.exec_cmd("~/.local/bin/rice-switcher apply")
end)
```

Do **not** call it from a top-level `exec`. Top-level `exec` re-runs on every `hyprctl reload`, while `hyprland.start` fires once per session.

---

## Known limitations

- **Config swap only.** The tool currently swaps symlinks and snapshots; it does not manage processes, themes, hooks, or colors. See [Not yet implemented](#not-yet-implemented).
- **Not fully transactional.** Rollback restores files and symlinks but cannot undo hook side effects or reverse a matugen run (the tool performs neither yet).
- **Hyprland validation is syntax-only.** `--verify-config` can't see missing files referenced from your config; a basic `require(...)` check is added on top.
- **Duplicate processes.** If two identical `qs -c ii` instances run at once, the tool can't tell which one it should stop.
- **Missing `hyprlock` helpers.** Rices that enable `hyprlock` must ship the helper scripts referenced by `hyprlock.conf`; validation only warns if they are missing.
- **Dangling GTK 4 symlink.** If `~/.config/gtk-4.0` is a symlink to a theme directory that doesn't exist, tools writing through it fail. Create the target directory or remove the symlink.
- **`hl.on` is a Hyprland extension.** The autostart approach relies on Hyprland's Lua event API, which may change between releases.
- Quickshell's own state (`~/.local/state/quickshell`, including its Python venv) is outside the scope of rice switching.

---

## License

MIT. See [`LICENSE`](LICENSE).
