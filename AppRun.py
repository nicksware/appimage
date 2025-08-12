#!/usr/bin/env python3
import os, subprocess, sys

appdir = os.path.dirname(os.path.realpath(__file__))

# sanity check on host deps
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw  # noqa: F401
except Exception as e:
    sys.stderr.write(
        "Missing GTK4/Libadwaita Python bindings on the host.\n"
        "Fedora: sudo dnf install -y python3 python3-gobject gtk4 libadwaita\n"
        "Debian/Ubuntu: sudo apt install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0\n"
        "Arch: sudo pacman -Sy python python-gobject gtk4 libadwaita\n"
        "Alpine: doas apk add python3 py3-gobject gtk4 libadwaita\n"
    )
    sys.exit(127)

sys.exit(subprocess.call([sys.executable, f"{appdir}/usr/bin/app.py"] + sys.argv[1:]))
