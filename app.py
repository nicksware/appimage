#!/usr/bin/env python3
# Pass GUI for Libadwaita/GTK4 (GNOME 48-friendly)
# - Uses host's pass OR podman/docker container (ghcr.io/noobping/pass:latest)
# - Async subprocess so UI stays responsive
# - No popups: uses banners/toasts + inline views
# - Settings page embedded (no separate window)
# - List all passwords (flat) and navigate folders; search; open/show entries

import os
import sys
import json
import shutil
import pathlib
import functools
import typing as t

import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk, Gio, GLib, Gdk

APP_ID = "org.example.PassGUI"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "passgui")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_STORE = os.path.expanduser("~/.password-store")

# --------------------------- Utilities ---------------------------

def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    else:
        cfg = {}
    # Defaults
    cfg.setdefault("backend", auto_backend())  # host|podman|docker|custom
    cfg.setdefault("custom_cmd", "pass")
    cfg.setdefault("store_path", DEFAULT_STORE)
    cfg.setdefault("software_fallback", True)
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def auto_backend() -> str:
    if have("pass"):
        return "host"
    if have("podman"):
        return "podman"
    if have("docker"):
        return "docker"
    return "custom"


# --------------------------- Pass Runner ---------------------------

class PassRunner:
    """Builds and runs the right 'pass' command based on backend.

    Modes:
      - host   : use system 'pass'
      - podman : use ghcr.io/noobping/pass:latest with podman
      - docker : same with docker
      - custom : a custom command string, e.g. "/usr/local/bin/pass"
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg

    # --- Command builders ---
    def _host_cmd(self, args: t.List[str]) -> t.List[str]:
        return ["pass", *args]

    def _custom_cmd(self, args: t.List[str]) -> t.List[str]:
        # Allow a whole custom command string (split by shell-like)
        import shlex
        base = shlex.split(self.cfg.get("custom_cmd", "pass"))
        return [*base, *args]

    def _container_cmd(self, engine: str, args: t.List[str]) -> t.List[str]:
        uid = os.getuid()
        gid = os.getgid()
        home = str(pathlib.Path.home())
        store = self.cfg.get("store_path", DEFAULT_STORE)
        # Mount chosen store to /home/app/.password-store inside container
        mounts = ["-v", f"{store}:/home/app/.password-store"]
        # Mount gnupg for decrypt
        gpg = os.path.join(home, ".gnupg")
        if engine == "podman":
            # SELinux relabel on Fedora with :Z
            mounts = ["-v", f"{store}:/home/app/.password-store:Z"] + (
                ["-v", f"{gpg}:/home/app/.gnupg:Z"] if os.path.isdir(gpg) else []
            )
        else:  # docker
            mounts = ["-v", f"{store}:/home/app/.password-store"] + (
                ["-v", f"{gpg}:/home/app/.gnupg"] if os.path.isdir(gpg) else []
            )

        base = [
            engine, "run", "--rm",
            "--user", f"{uid}:{gid}",
            "-e", "HOME=/home/app",
            # No TTY in GUI; gpg-agent should handle prompts if needed
            "-w", "/home/app",
            *mounts,
            "ghcr.io/noobping/pass:latest",
            "pass",
        ]
        return [*base, *args]

    def build(self, args: t.List[str]) -> t.List[str]:
        backend = self.cfg.get("backend", "host")
        if backend == "host":
            return self._host_cmd(args)
        if backend == "podman":
            return self._container_cmd("podman", args)
        if backend == "docker":
            return self._container_cmd("docker", args)
        # custom
        return self._custom_cmd(args)


# --------------------------- Async subprocess helper ---------------------------

def run_command_async(argv: t.List[str], *, cancellable: Gio.Cancellable | None = None,
                      on_done: t.Callable[[int, str, str], None] | None = None) -> None:
    """Run argv asynchronously; call on_done(returncode, out, err) in main loop."""
    try:
        proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except Exception as e:
        GLib.idle_add(lambda: on_done and on_done(-1, "", str(e)))
        return

    def _after_communicate(proc: Gio.Subprocess, res: Gio.AsyncResult, _data):
        try:
            ok, out_bytes, err_bytes = proc.communicate_finish(res)
            out = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
            err = err_bytes.decode("utf-8", errors="replace") if err_bytes else ""
            rc = 0 if ok and proc.get_successful() else proc.get_exit_status()
        except Exception as e:  # decoding or finish error
            rc, out, err = -1, "", str(e)
        if on_done:
            GLib.idle_add(on_done, rc, out, err)

    proc.communicate_async(None, cancellable, _after_communicate, None)


# --------------------------- Models ---------------------------

class Entry:
    def __init__(self, name: str, path: pathlib.Path):
        self.name = name  # pass key, e.g. "email/gmail"
        self.path = path  # .gpg file path

    def __repr__(self):
        return f"Entry({self.name})"


# --------------------------- Main Window ---------------------------

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, cfg: dict):
        super().__init__(application=app, title="Pass GUI", default_width=980, default_height=680)
        self.cfg = cfg
        self.runner = PassRunner(cfg)
        self.cancellable = Gio.Cancellable()

        # Optional GLES fallback
        if self.cfg.get("software_fallback", True):
            try:
                # Probe libGLESv2 presence cheaply; if absent, use Cairo renderer
                import subprocess
                res = subprocess.run(["/sbin/ldconfig", "-p"], capture_output=True, text=True)
                if "libGLESv2.so.2" not in res.stdout:
                    os.environ.setdefault("GSK_RENDERER", "cairo")
            except Exception:
                pass

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # Toolbar + ViewSwitcher
        self.toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(self.toolbar)

        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)

        self.view_stack = Adw.ViewStack()
        self.toolbar.set_content(self.view_stack)

        # Pages
        self.page_all = self._build_all_page()
        self.view_stack.add_titled(self.page_all, "all", "All")

        self.page_folders = self._build_folders_page()
        self.view_stack.add_titled(self.page_folders, "folders", "Folders")

        self.page_settings = self._build_settings_page()
        self.view_stack.add_titled(self.page_settings, "settings", "Settings")

        # View switcher in title
        self.switcher_title = Adw.ViewSwitcherTitle()
        # Avoid deprecated setter; assign the property directly
        self.switcher_title.props.stack = self.view_stack
        self.header.set_title_widget(self.switcher_title)

        # Search entry (applies to All list)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search passwords…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.header.pack_end(self.search_entry)

        # Initial state
        self.entries: list[Entry] = []
        self.filtered_entries: list[Entry] = []

        self._check_environment()
        self._refresh_lists()

    # ----------------- UI builders -----------------

    def _make_banner(self, text: str, actions: list[tuple[str, t.Callable[[], None]]]) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        banner = Adw.Banner.new(text)
        banner.set_revealed(True)
        row.append(banner)
        for label, cb in actions:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, f=cb: f())
            row.append(btn)
        return row

    def _build_all_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

        self.all_liststore = Gio.ListStore(item_type=GObjectEntry)
        self.all_selection = Gtk.SingleSelection(model=self.all_liststore)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._row_setup)
        factory.connect("bind", self._row_bind)
        listview = Gtk.ListView(model=self.all_selection, factory=factory)
        listview.connect("activate", self._on_all_activate)

        # Details viewer
        self.detail_title = Gtk.Label(xalign=0)
        self.detail_body = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.copy_btn = Gtk.Button(label="Copy password")
        self.copy_btn.connect("clicked", self._copy_primary)
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        detail_box.append(self.detail_title)
        detail_box.append(self.detail_body)
        detail_box.append(self.copy_btn)

        paned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        paned.set_start_child(listview)
        paned.set_end_child(detail_box)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(True)
        paned.set_position(380)

        box.append(paned)
        return box

    def _build_folders_page(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

        # Breadcrumb
        self.folder_path: list[str] = []
        self.breadcrumb = Gtk.Box(spacing=6)
        root.append(self.breadcrumb)

        # List
        self.folder_items: list[tuple[str, bool]] = []  # (name, is_dir)
        self.folder_store = Gio.ListStore(item_type=GObjectFolderItem)
        self.folder_sel = Gtk.SingleSelection(model=self.folder_store)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._folder_setup)
        factory.connect("bind", self._folder_bind)
        view = Gtk.ListView(model=self.folder_sel, factory=factory)
        view.connect("activate", self._on_folder_activate)

        # Details (same as All)
        self.detail_title2 = Gtk.Label(xalign=0)
        self.detail_body2 = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.copy_btn2 = Gtk.Button(label="Copy password")
        self.copy_btn2.connect("clicked", self._copy_primary2)
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        detail_box.append(self.detail_title2)
        detail_box.append(self.detail_body2)
        detail_box.append(self.copy_btn2)

        paned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        paned.set_start_child(view)
        paned.set_end_child(detail_box)
        paned.set_position(380)

        root.append(paned)
        return root

    def _build_settings_page(self) -> Gtk.Widget:
        outer = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        outer.set_child(box)

        # Backend selection
        box.append(Gtk.Label(label="Backend", xalign=0))
        backend_row = Gtk.Box(spacing=12)
        options = ["host", "podman", "docker", "custom"]
        self.backend_model = Gtk.StringList.new(options)
        self.backend_dd = Gtk.DropDown(model=self.backend_model)
        try:
            sel = options.index(self.cfg.get("backend", "host"))
        except ValueError:
            sel = 0
        self.backend_dd.set_selected(sel)
        backend_row.append(self.backend_dd)
        box.append(backend_row)

        # Custom command
        box.append(Gtk.Label(label="Custom pass command", xalign=0))
        self.custom_entry = Gtk.Entry()
        self.custom_entry.set_placeholder_text("e.g. /opt/pass/bin/pass")
        box.append(self.custom_entry)

        # Store path
        box.append(Gtk.Label(label="Password store folder", xalign=0))
        path_row = Gtk.Box(spacing=6)
        self.store_entry = Gtk.Entry()
        self.store_entry.set_text(self.cfg.get("store_path", DEFAULT_STORE))
        choose_btn = Gtk.Button(label="Choose…")
        choose_btn.connect("clicked", self._choose_store_folder)
        path_row.append(self.store_entry)
        path_row.append(choose_btn)
        box.append(path_row)

        # Git clone
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(Gtk.Label(label="Restore from Git", xalign=0))
        git_row = Gtk.Box(spacing=6)
        self.git_entry = Gtk.Entry()
        self.git_entry.set_placeholder_text("https://… or git@…")
        clone_btn = Gtk.Button(label="Clone into store")
        clone_btn.connect("clicked", self._clone_repo)
        git_row.append(self.git_entry)
        git_row.append(clone_btn)
        box.append(git_row)

        # Renderer fallback
        self.fallback_check = Gtk.CheckButton(label="Fallback to software renderer if no GLES present")
        self.fallback_check.set_active(self.cfg.get("software_fallback", True))
        box.append(self.fallback_check)

        # Save
        save_btn = Gtk.Button(label="Save settings")
        save_btn.connect("clicked", self._save_settings)
        box.append(save_btn)

        return outer

    # ----------------- List item factories -----------------
    def _row_setup(self, _factory, list_item: Gtk.ListItem):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(xalign=0)
        title.add_css_class("title-4")
        box.append(title)
        list_item.set_child(box)

    def _row_bind(self, _factory, list_item: Gtk.ListItem):
        obj: GObjectEntry = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        title: Gtk.Label = box.get_first_child()
        title.set_text(obj.name)

    def _folder_setup(self, _factory, list_item: Gtk.ListItem):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = Gtk.Image()
        title = Gtk.Label(xalign=0)
        title.add_css_class("title-4")
        row.append(icon)
        row.append(title)
        list_item.set_child(row)

    def _folder_bind(self, _factory, list_item: Gtk.ListItem):
        obj: GObjectFolderItem = list_item.get_item()
        row: Gtk.Box = list_item.get_child()
        icon: Gtk.Image = row.get_first_child()
        title: Gtk.Label = icon.get_next_sibling()
        icon.set_from_icon_name("folder" if obj.is_dir else "key-symbolic")
        title.set_text(obj.name)

    # ----------------- Events -----------------
    def _on_search_changed(self, entry: Gtk.SearchEntry):
        query = entry.get_text().strip().lower()
        if not query:
            self.filtered_entries = self.entries[:]
        else:
            self.filtered_entries = [e for e in self.entries if query in e.name.lower()]
        self._reload_all_model()

    def _on_all_activate(self, _listview, pos: int):
        item: GObjectEntry = self.all_selection.get_selected_item()
        if not item:
            return
        self._show_entry(item.name, viewer=1)

    def _on_folder_activate(self, _listview, pos: int):
        item: GObjectFolderItem = self.folder_sel.get_selected_item()
        if not item:
            return
        if item.is_dir:
            self.folder_path.append(item.name)
            self._update_breadcrumb()
            self._load_folder_items()
        else:
            full = "/".join(self.folder_path + [item.name])
            self._show_entry(full, viewer=2)

    def _copy_primary(self, _btn):
        buf: Gtk.TextBuffer = self.detail_body.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        if text:
            clipboard = self.get_display().get_clipboard()
            clipboard.set_text(text)
            self._toast("Copied to clipboard")

    def _copy_primary2(self, _btn):
        buf: Gtk.TextBuffer = self.detail_body2.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        if text:
            clipboard = self.get_display().get_clipboard()
            clipboard.set_text(text)
            self._toast("Copied to clipboard")

    # ----------------- State/helpers -----------------
    def _toast(self, text: str):
        self.toast_overlay.add_toast(Adw.Toast.new(text))

    def _warn_banner(self, text: str, actions: list[tuple[str, t.Callable[[], None]]]):
        banner = self._make_banner(text, actions)
        # show on Settings page header area
        # Simpler: add a transient banner on the current page root
        try:
            # place at the top of the current page if it's a Box
            page = self.view_stack.get_visible_child()
            if isinstance(page, Gtk.Box):
                page.prepend(banner)
            else:
                self.toast_overlay.add_toast(Adw.Toast.new(text))
        except Exception:
            self.toast_overlay.add_toast(Adw.Toast.new(text))

    def _check_environment(self):
        # Backend availability
        backend = self.cfg.get("backend", "host")
        missing = []
        if backend == "host" and not have("pass"):
            missing.append("pass")
        if backend == "podman" and not have("podman"):
            missing.append("podman")
        if backend == "docker" and not have("docker"):
            missing.append("docker")
        if missing:
            self._warn_banner(
                f"Missing: {', '.join(missing)}. Configure a different backend or set a custom command.",
                [
                    ("Open Settings", lambda: self.view_stack.set_visible_child(self.page_settings)),
                ],
            )

        # Password store presence
        store = self.cfg.get("store_path", DEFAULT_STORE)
        if not os.path.isdir(store):
            def choose():
                self._choose_store_folder(None)
            def restore():
                self.view_stack.set_visible_child(self.page_settings)
                self.git_entry.grab_focus()
            self._warn_banner(
                f"Password store not found at {store}.",
                [("Choose folder…", choose), ("Restore from Git…", restore)],
            )

    def _refresh_lists(self):
        # Build entries from store by scanning *.gpg
        store = pathlib.Path(self.cfg.get("store_path", DEFAULT_STORE))
        items: list[Entry] = []
        if store.is_dir():
            for p in store.rglob("*.gpg"):
                rel = p.relative_to(store).as_posix()
                name = rel[:-4] if rel.endswith(".gpg") else rel
                items.append(Entry(name=name, path=p))
        items.sort(key=lambda e: e.name.lower())
        self.entries = items
        self.filtered_entries = items[:]
        self._reload_all_model()
        self.folder_path = []
        self._update_breadcrumb()
        self._load_folder_items()

    def _reload_all_model(self):
        self.all_liststore.remove_all()
        for e in self.filtered_entries:
            self.all_liststore.append(GObjectEntry(e.name))

    def _update_breadcrumb(self):
        # Rebuild breadcrumb buttons
        for child in list(self.breadcrumb):
            self.breadcrumb.remove(child)
        # Root button
        root_btn = Gtk.Button(label="Password Store")
        root_btn.add_css_class("flat")
        root_btn.connect("clicked", lambda _b: self._nav_to([]))
        self.breadcrumb.append(root_btn)
        # Path buttons
        parts = []
        for i, part in enumerate(self.folder_path):
            parts.append(part)
            btn = Gtk.Button(label=part)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, upto=i+1: self._nav_to(self.folder_path[:upto]))
            self.breadcrumb.append(btn)

    def _nav_to(self, parts: list[str]):
        self.folder_path = parts
        self._update_breadcrumb()
        self._load_folder_items()

    def _load_folder_items(self):
        # List folders/files directly under current folder from entries
        prefix = "/".join(self.folder_path)
        seen_dirs = set()
        files = []
        for e in self.entries:
            if prefix and not e.name.startswith(prefix + "/"):
                continue
            rest = e.name[len(prefix)+1:] if prefix else e.name
            if "/" in rest:
                dir_name = rest.split("/", 1)[0]
                seen_dirs.add(dir_name)
            else:
                files.append(rest)
        items: list[tuple[str,bool]] = [(d, True) for d in sorted(seen_dirs)] + [(f, False) for f in sorted(files)]
        self.folder_store.remove_all()
        for name, is_dir in items:
            self.folder_store.append(GObjectFolderItem(name, is_dir))

    def _show_entry(self, name: str, *, viewer: int):
        # viewer 1: All page; viewer 2: Folders page
        args = ["show", name]
        argv = self.runner.build(args)
        self._toast(f"Loading {name}…")

        def done(rc: int, out: str, err: str):
            if rc != 0:
                self._toast(f"Error opening {name}")
                # Also show an inline banner on current page
                self._warn_banner(err.strip() or f"Failed to open {name}", [])
                return
            # First line is the password; show full content
            if viewer == 1:
                self.detail_title.set_text(name)
                buf: Gtk.TextBuffer = self.detail_body.get_buffer()
                buf.set_text(out)
            else:
                self.detail_title2.set_text(name)
                buf: Gtk.TextBuffer = self.detail_body2.get_buffer()
                buf.set_text(out)
            self._toast("Loaded.")

        run_command_async(argv, cancellable=self.cancellable, on_done=done)

    # ----------------- Settings actions -----------------
    def _choose_store_folder(self, _btn):
        # Use GTK4 file dialog (goes through portal if available)
        dialog = Gtk.FileDialog(title="Select password store folder")
        dialog.select_folder(self, None, lambda d, res: self._on_folder_chosen(d, res))

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, res: Gio.AsyncResult):
        try:
            file = dialog.select_folder_finish(res)
            path = file.get_path()
            if path:
                self.store_entry.set_text(path)
                self.cfg["store_path"] = path
                save_config(self.cfg)
                self._refresh_lists()
                self._toast("Store folder set.")
        except Exception as e:
            self._warn_banner(str(e), [])

    def _clone_repo(self, _btn):
        repo = self.git_entry.get_text().strip()
        if not repo:
            self._toast("Enter a Git URL")
            return
        dest = self.store_entry.get_text().strip() or DEFAULT_STORE
        os.makedirs(dest, exist_ok=True)
        argv = ["git", "clone", repo, dest]
        self._toast("Cloning…")

        def done(rc: int, out: str, err: str):
            if rc != 0:
                self._warn_banner(err.strip() or "git clone failed", [])
                return
            self._toast("Repository cloned")
            self._refresh_lists()

        run_command_async(argv, cancellable=self.cancellable, on_done=done)

    def _save_settings(self, _btn):
        sel = self.backend_dd.get_selected()
        self.cfg["backend"] = (self.backend_model.get_string(sel) if sel != Gtk.INVALID_LIST_POSITION else "host")
        self.cfg["custom_cmd"] = self.custom_entry.get_text().strip() or "pass"
        self.cfg["store_path"] = self.store_entry.get_text().strip() or DEFAULT_STORE
        self.cfg["software_fallback"] = self.fallback_check.get_active()
        save_config(self.cfg)
        self.runner = PassRunner(self.cfg)
        self._toast("Settings saved")
        self._check_environment()
        self._refresh_lists()


# --------------------------- GObject wrappers for ListStores ---------------------------

# Gtk.ListView expects GObject types; provide minimal wrappers

gi.require_version("GObject", "2.0")
from gi.repository import GObject

class GObjectEntry(GObject.GObject):
    name = GObject.Property(type=str, default="")
    def __init__(self, name: str):
        super().__init__()
        self.name = name

class GObjectFolderItem(GObject.GObject):
    name = GObject.Property(type=str, default="")
    is_dir = GObject.Property(type=bool, default=False)
    def __init__(self, name: str, is_dir: bool):
        super().__init__()
        self.name = name
        self.is_dir = is_dir


# --------------------------- Application ---------------------------

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        Adw.init()

    def do_activate(self):
        cfg = load_config()
        win = self.props.active_window
        if not win:
            win = MainWindow(self, cfg)
        win.present()


def main(argv=None):
    app = App()
    return app.run(argv or sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
