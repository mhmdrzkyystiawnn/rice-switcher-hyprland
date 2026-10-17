# Auto start Hyprland on tty1
if status is-login
    and test -z "$DISPLAY"
    and test "$XDG_VTNR" -eq 1
    mkdir -p ~/.cache
    exec start-hyprland > ~/.cache/hyprland.log 2>&1
end
