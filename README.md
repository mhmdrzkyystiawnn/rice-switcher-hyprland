# rice-switcher-hyprland

> Switch between Hyprland "rices" by swapping config symlinks and restarting the desktop components — safely, with snapshots, rollback, and a dry-run-by-default workflow.

![status](https://img.shields.io/badge/status-slices%201--5%20implemented-blue)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

Hyprland has no built-in way to swap between whole desktop setups. If you keep several rices (for example a Quickshell-based one and a minimal Waybar one), switching by hand means swapping a pile of config directories and hoping nothing breaks.

**rice-switcher** records each rice in a `rice.toml` manifest, then swaps the `config_link` symlinks and runs the component lifecycle (hooks, stop/start, `[theme]`, Hyprland reload) for you, with a snapshot taken before every change so you can roll back.

> **Project status:** the CLI implements Slices 1–5 (see [Roadmap](#roadmap)). Everything documented below describes what the code does *today*. Features that are designed but **not** implemented are collected in [Not yet implemented](#not-yet-implemented); do not assume they exist.

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
- **Process lifecycle:** `switch` runs hooks, stops/starts the managed components (verified by identity-aware probing), applies `[theme]`, reloads Hyprland, and regenerates colors with matugen (docs/PLAN.md §3.2). `RICE_TEST=1` keeps it to the symlink swap.
- **Identity-aware process probing:** process identity is `(executable, argv_match)`, so `qs -c ii` and `qs -c minimal` are distinguished. Used by `status` to report running PIDs and by the lifecycle to decide whether a component is up.
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
2. **Validate** the target manifest (`validate_manifest`). `config_link` conflicts / escape, a missing `start` for a non-optional component, and a `pkill -f`/`--full` command anywhere are errors; missing binaries and other issues are warnings.
3. **No-op check** — if the target is already the current rice, print a no-op plan and stop.
4. **Compute the plan** — `to_stop`, `to_start`, `to_reload`, `to_swap`, `to_keep` — from the managed set and the current symlink state.
5. **Dry-run** (the default) prints the plan and returns without touching anything.
6. **Acquire the lock** (5-second timeout).
7. **Snapshot** the current rice's `config_link` targets into `snapshot/.pending-<ts>/` (recording the current rice name in `.captured.txt`).
8. **Run `[hooks] pre`** (each through `bash -lc`).
9. **Stop** the components in `to_stop` (their `stop` command, then wait for them to disappear; `stop_check` or a `/proc` scan).
10. **Apply `[theme]`** — cursor via `hyprctl setcursor`, GTK/icon/fonts via `gsettings`, then the optional `theme.apply`.
11. **Swap each `config_link`** to the target rice's copy; the previous entry is renamed to `<path>.bak-<ts>`.
12. **Verify** the new Hyprland config (`hyprland --verify-config`), **reload** (`hyprctl reload` plus every command in `to_reload`), and **regenerate colors** (`matugen image <wallpaper>`).
13. **Start** the components in `to_start`: each `start` command is spawned **detached** (new session, so a daemon like `waybar` is not killed when the command returns), then `switch` waits for the process to appear (`start_check` or a `/proc` scan) and runs `health_check` if set.
14. **Run `[hooks] post`**.
15. **Commit** the pending snapshot, then prune old snapshots (keep 5).
16. **Update `state.json`** (`current_rice` + trust cache) and append an audit event per executed command.
17. If anything fails after step 7, the lifecycle is unwound best-effort (started components stopped, stopped components restarted), each `config_link` is restored (its `.bak-<ts>` backup via `undo_swap`, else the pending snapshot), Hyprland is reloaded if it had been reloaded, the pending snapshot is discarded, the trust entry is marked `rolled_back`, and the error is returned.

The compositor (`type = "compositor"`) is never stopped or started. Steps 8–14 execute real commands and are **on by default**; set `RICE_TEST=1` (or `--dry-run`) to skip them.

### What `switch` does **not** do

- **Undo irreversible steps.** `[hooks]`, `theme.apply` and matugen cannot be undone; rollback restores files/symlinks and best-effort restarts processes, but does not reverse those.
- **Guarantee every stopped process returns.** A component whose `start` is broken will not come back; check the audit log.
- **Touch components outside the managed set** (`gnome-keyring`, `geoclue`, `ydotool`, `cliphist_*`, …) — those are never managed.
- **Stop a desktop it does not know about.** `switch` only stops the components declared by the *current* rice. If the running session was never registered (`state.current_rice` empty), the plan has nothing to stop and the target's components are layered on top of the existing ones. Register the running session first with `rice-switcher adopt <name>` (a manifest that declares the components you already run, e.g. a `quickshell` component with `executable = "qs"`, `argv_match = ["-c", "ii"]`), then `switch` will stop them — and `rollback` will start them again.

See [Not yet implemented](#not-yet-implemented) for the rest of the roadmap.

---

## Requirements

**Required**

- Python **3.11+** (uses `tomllib`). No runtime dependencies.

**Used only by specific commands**

- `hyprctl` — needed by `apply --require-session` to probe for a live Hyprland session, and by `switch` to apply the cursor (`hyprctl setcursor`) and reload Hyprland.
- `hyprland` — needed by `validate` and by `switch` to run `hyprland --verify-config`.
- `gsettings` — needed by `switch` to apply `[theme]` GTK/icon/font values.
- `matugen` — needed by `switch` to regenerate colors when the target rice sets `wallpaper`.
- `bash` — needed by `switch` to run `[hooks]`, `theme.apply` and any `start`/`stop` command that uses shell syntax.
- An editor (`$EDITOR` / `$VISUAL` / `vi`) — needed by `edit`.
- `fuzzel` or `rofi` — needed by `menu` (either one).

Binaries named in a component's `start` are looked up on `PATH` by validation (a warning if missing) and, on `switch`, actually executed for the components in the plan.

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
| `rice-switcher adopt <name>` | Record the **running** desktop as `<name>` without swapping anything, so the next `switch` knows which components to stop. Use it once for a rice that was already installed by hand. |
| `rice-switcher status` | Table per rice: symlink state, symlink owner, probed component PIDs, drift, last-switch time. |
| `rice-switcher validate <name>` | Deep validation: schema, binaries, `config_link`, Hyprland config, matugen templates. Exit 1 if errors. |
| `rice-switcher switch <name> [--dry-run] [--no-dry-run]` | Switch to a rice. See [How it works](#what-switch-does-today). Default dry-run. |
| `rice-switcher snapshot` | Take a manual snapshot of the current rice's `config_link` targets. |
| `rice-switcher rollback [ts]` | Restore a snapshot (latest if omitted) and re-sync `state.json`. |
| `rice-switcher apply [--require-session]` | Re-apply the rice recorded in `state.json`. Intended for `hl.on("hyprland.start", ...)`. Runs a real swap without the confirmation prompt. |
| `rice-switcher import <path> [--name N] [--apply]` | Import an existing config directory as a new rice. Default dry-run; `--apply` copies. Read-only with respect to your live desktop. |
| `rice-switcher clone <url> [--name N] [--depth D] [--branch B] [--allow-local] [--yes]` | Clone a third-party rice repo into `<RICE_HOME>/<name>` (name derived from the URL by default). Hardened: argv only (no shell), `--` before the URL, git protocol allowlist, no credential prompts. Writes `<name>/.rice-source.toml` provenance (URL redacted) and warns about symlinks that escape the rice dir. Does **not** create `rice.toml` or touch state. |
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

[theme]                              # applied by `switch` (cursor/gsettings/apply)
cursor_name = "Bibata-Modern-Classic"
cursor_size = 24
gtk_theme = "Wallbash-Gtk"
icon_theme = "Tela-circle-dracula"
font_main = "Google Sans Flex"
font_mono = "JetBrains Mono Nerd Font"
# apply = "~/rice/illogical-impulse/scripts/apply-theme.sh"   # optional custom command

[hooks]                              # run by `switch` pre/post (via `bash -lc`)
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

`pkill -f` / `pkill --full` is considered dangerous because it matches the full command line (including the switcher itself). `validate_manifest` rejects it (an **error**) in any `start` / `stop` / `reload` / `start_check` / `stop_check` / `health_check`, in `[hooks]`, and in `theme.apply`. Use `pkill -x` (exact process name) instead.

### Component fields

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique identifier within the rice; used by `depends_on`. |
| `type` | yes | `compositor`, `shell`, `bar`, `notifications`, `locker`, `wallpaper`, `terminal`, `launcher`, `theme_generator`, `color_daemon`, `clipboard` or `system`. |
| `start` | yes (unless `optional`) | Command to start the component. `true` is a no-op. Validation only checks the first word against `PATH`. `switch` spawns it detached and then polls for the process. |
| `stop` | no | Command to stop the component. Defaults to `true`. |
| `executable` | no | Executable basename for process identity (defaults to `name`). |
| `argv_match` | no | Tokens that must appear in the process argv, to tell apart e.g. `qs -c ii` from `qs -c minimal`. |
| `reload` | no | Live-reload command, run by `switch` when the component's config changed. |
| `config_link` | no | Path symlinked to the rice's copy. Only components that set it are swapped. |
| `depends_on` | no | Components that must start first (recording only). |
| `start_check` / `stop_check` | no | Command that exits 0 to confirm up / down. Defaults to a `/proc` identity scan. |
| `health_check` | no | Command that must exit 0 after `start`; a failure aborts the switch. |
| `optional` | no | Exempt from the "missing binary" and "missing start" checks; a failed `stop`/`start` is skipped instead of aborting. |
| `timeout` | no | Non-negative integer (default 10) — per-command timeout and how long to wait for the process to appear/disappear. |
| `match` | no | Free-form match string (recording only). |

### `[theme]` fields

`cursor_name`, `cursor_size`, `gtk_theme`, `icon_theme`, `font_main`, `font_mono`, `font_size`, `apply`. All optional. `switch` applies them: cursor via `hyprctl setcursor`, GTK/icon/fonts via `gsettings`, then `apply` (a shell command) last. `apply` failing aborts the switch; the individual `gsettings`/`hyprctl` calls are best-effort.

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
├── docs/
│   └── PLAN.md             # public spec (design document)
├── LICENSE
└── README.md
```

Runtime state lives in `<RICE_STATE_HOME>/rice-switcher/`: `state.json`, `switch.lock`, `snapshot/`, `logs/switcher.log`, `logs/audit.log`.

---

## Safety model

**Snapshots.** Before any swap, every `config_link` target of the *current* rice is copied into `snapshot/.pending-<ts>/` (symlinks preserved as symlinks, dangling links included; `shutil.copytree(symlinks=True)`, never dereferenced). On success it is renamed to `snapshot/<ts>/` and `snapshot/latest` is updated. The 5 most recent snapshots are kept. Each snapshot stores a `.captured.txt` listing the captured paths and the rice name.

**Rollback.** `switch` rolls back automatically if a step fails after the snapshot is taken: started components are stopped and stopped components are restarted (best-effort), each `config_link` is restored (preferring the `.bak-<ts>` backup made by the swap, falling back to the pending snapshot), and — if Hyprland had already been reloaded — the restored config is reloaded back into the running compositor. The pending snapshot is discarded and `state.json` marks the rice `rolled_back`. `rice-switcher rollback [ts]` restores a committed snapshot manually: it stops the components the current rice started and starts the components the restored rice needs (best-effort, using the manifests), re-syncs `current_rice` (a present-but-empty `rice=` line resets it to "no rice"; a snapshot with no `rice=` line at all is reported as an error rather than guessed), and reloads Hyprland when the restored paths include the compositor config. Rollback restores **files, symlinks and (best-effort) processes**; it cannot undo `[hooks]`, `theme.apply` or a matugen run.

**Hyprland config validation.** The `validate` command runs `hyprland --verify-config --config <path>` (a non-zero result or timeout is an error; a missing `hyprland` binary is a warning). `switch` validates the manifest too (including the basic `require("...")` / `source = ...` referenced-path check on `hyprland.lua`, warnings only) and, after swapping a `hyprland` `config_link`, runs `hyprland --verify-config` again; a failure there triggers the rollback. `--verify-config` checks syntax only.

**Locking.** `flock(2)` on `switch.lock` with a 5-second timeout. A second switch while one is running aborts without changing anything.

---

## Security

A `rice.toml` can run shell commands on your machine. `switch` (and `apply`) execute `start`, `stop`, `reload`, `hooks.*` and `theme.apply` — treat a rice from someone else like any script you download and review it before switching.

- `import` is read-only: it parses the manifest and copies files, and never executes anything from the source manifest.
- The first `switch` to a rice prints the plan and requires typing `yes` (unless the rice is already trusted and its manifest hash is unchanged).
- Approval is tied to the manifest's SHA-256. If the manifest changes, you are asked again.
- The tool's own subprocess calls (editor, picker, `hyprland --verify-config`, component lifecycle) use argv lists, never `eval`. Validation rejects `pkill -f` / `pkill --full` (too broad, and it matches the switcher itself) in any lifecycle command, hook, or `theme.apply`.
- Actions are recorded in a JSON Lines audit log (`logs/audit.log`). Sensitive keys (`api_key`, `token`, `secret`, …) and secret-shaped values are redacted before writing. There is no auto-delete; old `audit-*.log` archives are gzipped once older than `RICE_AUDIT_ROTATE_DAYS` (default 30).
- Each executed lifecycle command is recorded in the audit log with its exit code (`action: "exec"`), alongside the higher-level events (switch, rollback, import, edit, keybind-install, …).

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
| `RICE_TEST=1` | Disable lifecycle execution (hooks/stop/theme/reload/matugen/start) so only the symlink swap runs. Used by the test suite. |

The same path overrides can be set per rice in the `[run]` table of `rice.toml` (`rice_home`, `config_home`, `state_home`).

---

## Matugen and generated files

`rice-switcher validate` only *validates* a rice's matugen config (TOML well-formedness and template `input_path` / `output_path` checks). During `switch`, if the target rice sets `wallpaper`, the tool runs `matugen image <wallpaper>` to regenerate colors (a failure is logged, not fatal).

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

The suite uses `unittest` and runs hermetically (everything under temporary directories; it does not start or stop processes, reload Hyprland, or write to `~/.config`). Process-management tests inject a fake `Runtime`; the rest of the suite sets `RICE_TEST=1` so no real command is spawned.

```bash
# one file
python3 tests/test_slice1.py

# everything
for f in tests/test_*.py tests/design/test_*.py; do python3 "$f"; done
```

Note: `python3 -m unittest discover -s tests` does **not** work because `tests/` is not an importable package (no `__init__.py`); use one of the commands above.

The suite covers managed-set computation, `config_link` conflict detection, process identity, the switch plan (including "compositor never stopped" and "shared component not restarted"), dry-run purity, snapshot and rollback with symlinks, locking, the JSON Lines audit log, the Slice 2–4 commands (`validate`, `status`, `apply`, `menu`, `keybind-install`, `import`, `diff`, `edit`), Slice 5 process management (`executor`, `lifecycle`, hooks/stop/theme/reload/start, process rollback), and a dedicated design test file.

---

## Not yet implemented

These are designed in `docs/PLAN.md` but not implemented in the code:

- **`random`** and **`remove`** subcommands.
- **systemd integration** for managed components (docs/PLAN.md §5.2 chose direct subprocess instead).

---

## Roadmap

| Slice | Scope | Status |
|---|---|---|
| **1. Core** | `init`, `add`, `list`, `current`, `status`, `switch` (+ `--dry-run`), `snapshot`, `rollback`; validation; managed set; locking | Implemented |
| **2. UX** | `validate`, richer `status`, audit log + rotation, `keybind-install` | Implemented |
| **3. Automation** | `menu`, `apply` | Implemented |
| **3. Automation (rest)** | `random`, autostart migration, notifications | Not implemented |
| **5. Lifecycle** | hooks, stop/start (identity-aware), `[theme]`, `hyprctl reload`, matugen, process rollback | Implemented |
| **4. Sharing** | `import`, `diff`, `edit` | Implemented |
| **4. Sharing (rest)** | `remove`, optional systemd integration | Not implemented |
| **6. Onboarding** | `clone` + naming (Fase 1); auto-`rice.toml` draft, gap detection, TUI to follow | Fase 1 implemented |

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

- **Processes are managed best-effort.** `switch` starts/stops the managed components and reloads Hyprland, but a broken `start` cannot resurrect a component, and two identical processes (`qs -c ii`) cannot be told apart.
- **Not fully transactional.** Rollback restores files, symlinks and (best-effort) processes, but cannot undo hook side effects or reverse a matugen run.
- **Hyprland validation is syntax-only.** `--verify-config` can't see missing files referenced from your config; a basic `require(...)` check is added on top.
- **Duplicate processes.** If two identical `qs -c ii` instances run at once, the tool can't tell which one it should stop.
- **Missing `hyprlock` helpers.** Rices that enable `hyprlock` must ship the helper scripts referenced by `hyprlock.conf`; validation only warns if they are missing.
- **Dangling GTK 4 symlink.** If `~/.config/gtk-4.0` is a symlink to a theme directory that doesn't exist, tools writing through it fail. Create the target directory or remove the symlink.
- **`hl.on` is a Hyprland extension.** The autostart approach relies on Hyprland's Lua event API, which may change between releases.
- Quickshell's own state (`~/.local/state/quickshell`, including its Python venv) is outside the scope of rice switching.

---

## License

MIT. See [`LICENSE`](LICENSE).
