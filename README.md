# rice-switcher

CLI tool untuk pindah antar *rice* di Hyprland dengan manajemen proses
komponen (Quickshell, Waybar, dll.), warna/tema via matugen, dan symlink
atomic untuk swap config.

Lihat `PLAN.md` untuk desain lengkap.

## Status

Slice 1 (MVP): `--dry-run`, `init`, `add`, `list`, `current`, `status`,
`switch`, `snapshot`, `rollback`. Slice 2+ belum.

## Cara pakai (Slice 1)

```sh
# inisialisasi direktori rice & state di /tmp untuk testing
RICE_HOME=/tmp/rice-test RICE_STATE_HOME=/tmp/rice-state \
  python3 -m rice_switcher init

# lihat status
python3 -m rice_switcher status

# switch (default --dry-run)
python3 -m rice_switcher switch illogical-impulse
python3 -m rice_switcher switch waybar-minimal --no-dry-run
```

## Environment variables

| Var | Default | Fungsi |
|---|---|---|
| `RICE_HOME` | `~/rice` | root rice directory |
| `RICE_CONFIG_HOME` | `~/.config` (atau `$XDG_CONFIG_HOME`) | target symlink |
| `RICE_STATE_HOME` | `~/.local/state/rice-switcher` (atau `$XDG_STATE_HOME`) | state, snapshot, lock, logs |
| `RICE_LOG_FILE` | `$RICE_STATE_HOME/rice-switcher/logs/switcher.log` | log path |
| `RICE_TEST` | `0` | `1` untuk mode stub (no real command exec) |
| `RICE_DRY_RUN` | `0` | `1` untuk force dry-run semua subcommand |

## License

MIT. Lihat `LICENSE`.
