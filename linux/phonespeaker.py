#!/usr/bin/env python3
"""PhoneSpeaker - send your laptop's sound to your iPhone."""

from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from engine import PORT, Engine, local_ips  # noqa: E402

APP_ID = "io.github.phonespeaker.PhoneSpeaker"


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="PhoneSpeaker")
        self.set_default_size(420, 520)
        self.engine = Engine(on_change=lambda: GLib.idle_add(self.refresh))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        self.status = Adw.StatusPage(icon_name="phone-symbolic")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_size_request(320, -1)
        box.set_halign(Gtk.Align.CENTER)

        self.button = Gtk.Button()
        self.button.add_css_class("pill")
        self.button.add_css_class("suggested-action")
        self.button.set_halign(Gtk.Align.CENTER)
        self.button.connect("clicked", self.on_toggle)
        box.append(self.button)

        self.phones = Adw.PreferencesGroup(title="Connected phones")
        self.phone_rows: list[Gtk.Widget] = []
        box.append(self.phones)

        info = Adw.PreferencesGroup(
            title="Manual connection",
            description="If your iPhone doesn't find this laptop automatically, "
            "type one of these addresses into the app.",
        )
        for ip in local_ips() or ["(no network)"]:
            row = Adw.ActionRow(title=ip, subtitle=f"port {PORT}")
            row.add_css_class("property")
            row.set_subtitle_selectable(True)
            info.add(row)
        box.append(info)

        self.status.set_child(box)
        scroller = Gtk.ScrolledWindow(child=self.status)
        toolbar.set_content(scroller)
        self.set_content(toolbar)
        self.connect("close-request", self.on_close)
        self.refresh()

    def on_toggle(self, _btn: Gtk.Button) -> None:
        if self.engine.running:
            self.engine.stop()
        else:
            self.engine.start()
        self.refresh()

    def on_close(self, *_args) -> bool:
        self.engine.stop()
        return False

    def refresh(self) -> bool:
        e = self.engine
        for row in self.phone_rows:
            self.phones.remove(row)
        self.phone_rows = []

        if e.running:
            self.button.set_label("Stop — play on laptop again")
            self.button.remove_css_class("suggested-action")
            self.button.add_css_class("destructive-action")
            if e.clients:
                self.status.set_title("Playing on iPhone")
                self.status.set_description("Laptop speakers are silent.")
            else:
                self.status.set_title("Waiting for iPhone…")
                self.status.set_description(
                    "Laptop speakers are silent. Open PhoneSpeaker on your iPhone and tap Connect."
                )
            self.phones.set_visible(True)
            for addr in list(e.clients):
                row = Adw.ActionRow(title=addr[0], icon_name="phone-symbolic")
                self.phones.add(row)
                self.phone_rows.append(row)
            if not e.clients:
                row = Adw.ActionRow(title="None yet")
                self.phones.add(row)
                self.phone_rows.append(row)
        else:
            self.button.set_label("Send sound to iPhone")
            self.button.remove_css_class("destructive-action")
            self.button.add_css_class("suggested-action")
            self.status.set_title("Sound is on the laptop")
            self.status.set_description(e.error and f"Error: {e.error}" or
                                        "Start to send all laptop sound to your iPhone.")
            self.phones.set_visible(False)
        return False


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.win: Window | None = None

    def do_activate(self) -> None:
        if not self.win:
            self.win = Window(self)
        self.win.present()


def main() -> int:
    app = App()

    def _quit(*_a):
        if app.win:
            app.win.engine.stop()
        app.quit()

    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except (ImportError, ValueError):
        add_signal = GLib.unix_signal_add
    add_signal(GLib.PRIORITY_DEFAULT, signal.SIGINT, _quit)
    add_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _quit)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
