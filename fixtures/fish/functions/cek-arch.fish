function cek-arch
    echo "== Update tersedia =="
    checkupdates
    echo ""
    echo "== Service gagal =="
    systemctl --failed --no-pager
    echo ""
    echo "== Error boot (20 terakhir) =="
    journalctl -p 3 -b --no-pager | tail -n 20
    echo ""
    echo "== Disk =="
    df -h / /home
    echo ""
    echo "== Orphan =="
    pacman -Qdtq
    echo ""
    echo "== File .pacnew/.pacsave =="
    find /etc \( -name "*.pacnew" -o -name "*.pacsave" \) 2>/dev/null
end
