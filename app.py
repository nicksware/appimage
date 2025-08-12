#!/usr/bin/env python3
import gi
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk, Gio

APP_ID = "org.example.ModernDemo"

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Modern Demo", default_width=520, default_height=380)
        self.set_content(Adw.ToolbarView())
        header = Adw.HeaderBar()
        self.get_content().add_top_bar(header)

        btn = Gtk.Button(label="Say hello")
        btn.connect("clicked", self.on_click)

        clamp = Adw.Clamp()
        clamp.set_child(btn)

        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(clamp)
        self.toast_overlay = toast_overlay

        self.get_content().set_content(toast_overlay)

    def on_click(self, _btn):
        self.toast_overlay.add_toast(Adw.Toast.new("Hello from host Python + Libadwaita/GTK4!"))

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        Adw.init()

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = Window(self)
        win.present()

if __name__ == "__main__":
    app = App()
    app.run()
