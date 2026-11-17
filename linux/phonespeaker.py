#!/usr/bin/env python3
"""PhoneSpeaker - send your laptop's sound to your iPhone.

While this app runs, iPhones on the same network can find the laptop and ask to
join. You allow or deny each request here. Closing the window keeps it running
in the background; use Quit in the menu to stop it completely.
"""

from __future__ import annotations

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from engine import PORT, Engine, JoinRequest, local_ips  # noqa: E402

APP_ID = "io.github.phonespeaker.PhoneSpeaker"
AUTOSTART_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "autostart", f"{APP_ID}.desktop")


def autostart_enabled() -> bool:
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(on: bool) -> None:
    if on:
        os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
        with open(AUTOSTART_FILE, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=PhoneSpeaker\n"
                    f"Exec=python3 {os.path.realpath(__file__)} --background\n"
                    "Icon=phone\nX-GNOME-Autostart-enabled=true\n")
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: "App") -> None:
        super().__init__(application=app, title="PhoneSpeaker")
        self.app = app
        self.engine = app.engine
        self.set_default_size(440, 620)

        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Quit PhoneSpeaker", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self.status = Adw.StatusPage(icon_name="phone-symbolic")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_size_request(340, -1)
        box.set_halign(Gtk.Align.CENTER)

        self.connected = Adw.PreferencesGroup(title="Connected iPhones")
        self.remembered = Adw.PreferencesGroup(
            title="Remembered iPhones",
            description="These join without asking.")
        box.append(self.connected)
        box.append(self.remembered)
        self._rows: list[tuple[Adw.PreferencesGroup, Gtk.Widget]] = []

        settings = Adw.PreferencesGroup(title="Settings")
        login = Adw.SwitchRow(title="Start when I log in",
                              subtitle="So your iPhone can always find this laptop")
        login.set_active(autostart_enabled())
        login.connect("notify::active", lambda row, _p: set_autostart(row.get_active()))
        settings.add(login)
        box.append(settings)

        addr = Adw.ExpanderRow(title="Laptop address",
                               subtitle="Only needed if your iPhone can't find the laptop")
        for ip in local_ips() or ["(no network)"]:
            r = Adw.ActionRow(title=ip, subtitle=f"port {PORT}")
            r.set_title_selectable(True)
            addr.add_row(r)
        addr_group = Adw.PreferencesGroup()
        addr_group.add(addr)
        box.append(addr_group)

        self.status.set_child(box)
        toolbar.set_content(Gtk.ScrolledWindow(child=self.status))
        self.set_content(toolbar)
        self.connect("close-request", self.on_close)
        self.refresh()

    def on_close(self, *_args) -> bool:
        self.set_visible(False)
        self.app.notify_background()
        return True   # keep running so iPhones can still join

    def _clear_rows(self) -> None:
        for group, row in self._rows:
            group.remove(row)
        self._rows = []

    def _add(self, group: Adw.PreferencesGroup, row: Gtk.Widget) -> None:
        group.add(row)
        self._rows.append((group, row))

    def refresh(self) -> bool:
        e = self.engine
        self._clear_rows()
        clients = list(e.clients.values())

        if e.error:
            self.status.set_title("Something went wrong")
            self.status.set_description(e.error)
        elif clients and e.audio_on:
            names = ", ".join(c.name for c in clients)
            self.status.set_title(f"Playing on {names}")
            self.status.set_description("Laptop speakers are silent. Disconnect the iPhone to get sound back here.")
        elif e.pending:
            self.status.set_title("An iPhone wants to connect")
            self.status.set_description("Allow or deny it in the pop-up.")
        else:
            self.status.set_title("Ready for your iPhone")
            self.status.set_description(
                f"Open PhoneSpeaker on your iPhone and tap “{e.name}”. "
                "Sound plays here until an iPhone joins.")

        self.connected.set_visible(bool(clients))
        for c in clients:
            row = Adw.ActionRow(title=c.name, subtitle=c.addr[0], icon_name="phone-symbolic")
            btn = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER)
            btn.add_css_class("destructive-action")
            btn.connect("clicked", lambda _b, a=c.addr: e.kick(a))
            row.add_suffix(btn)
            self._add(self.connected, row)

        self.remembered.set_visible(bool(e.trusted))
        for did, name in e.trusted.items():
            row = Adw.ActionRow(title=name, icon_name="phone-symbolic")
            btn = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, d=did: e.forget(d))
            row.add_suffix(btn)
            self._add(self.remembered, row)
        return False


class App(Adw.Application):
    def __init__(self, background: bool) -> None:
        super().__init__(application_id=APP_ID)
        self.start_hidden = background
        self.win: Window | None = None
        self.dialogs: dict[str, Adw.AlertDialog] = {}
        self._told_background = False
        self.engine = Engine(
            on_change=lambda: GLib.idle_add(self._refresh),
            on_join_request=lambda req: GLib.idle_add(self._ask, req),
            on_request_gone=lambda did: GLib.idle_add(self._close_request, did),
        )

    # ---- lifecycle ----
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.hold()   # keep running with the window closed
        for name, cb, ptype in [
            ("quit", lambda *_: self.quit_all(), None),
            ("show", lambda *_: self.show_window(), None),
            ("allow", lambda _a, v: self._answer(v.get_string(), True, False), "s"),
            ("always", lambda _a, v: self._answer(v.get_string(), True, True), "s"),
            ("deny", lambda _a, v: self._answer(v.get_string(), False, False), "s"),
        ]:
            act = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            act.connect("activate", cb)
            self.add_action(act)
        self.win = Window(self)
        self.engine.listen()

    def do_activate(self) -> None:
        if self.start_hidden:
            self.start_hidden = False   # later launches from the app menu show the window
            return
        self.show_window()

    def show_window(self) -> None:
        if self.win:
            self.win.set_visible(True)
            self.win.present()

    def quit_all(self) -> None:
        self.engine.shutdown()
        self.release()
        self.quit()

    def notify_background(self) -> None:
        if self._told_background:
            return
        self._told_background = True
        n = Gio.Notification.new("PhoneSpeaker is still running")
        n.set_body("Your iPhone can still connect. Quit from the ☰ menu to stop it.")
        n.set_default_action("app.show")
        self.send_notification("background", n)

    def _refresh(self) -> bool:
        if self.win:
            self.win.refresh()
        return False

    # ---- join requests ----
    def _ask(self, req: JoinRequest) -> bool:
        did = req.device_id
        n = Gio.Notification.new(f"{req.name} wants to connect")
        n.set_body("Play this laptop's sound on that iPhone?")
        n.set_priority(Gio.NotificationPriority.HIGH)
        n.add_button_with_target("Deny", "app.deny", GLib.Variant.new_string(did))
        n.add_button_with_target("Allow", "app.allow", GLib.Variant.new_string(did))
        n.add_button_with_target("Always allow", "app.always", GLib.Variant.new_string(did))
        n.set_default_action("app.show")
        self.send_notification(f"join-{did}", n)

        if not self.win:
            return False
        dialog = Adw.AlertDialog(
            heading=f"{req.name} wants to connect",
            body=f"Play this laptop's sound on that iPhone ({req.addr[0]})? "
                 "The laptop speakers will go silent while it's connected.")
        remember = Gtk.CheckButton(label="Always allow this iPhone")
        dialog.set_extra_child(remember)
        dialog.add_response("deny", "Deny")
        dialog.add_response("allow", "Allow")
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("allow")
        dialog.set_close_response("deny")
        dialog.connect("response", lambda _d, resp: self._answer(
            did, resp == "allow", remember.get_active(), from_dialog=True))
        self.dialogs[did] = dialog
        self.show_window()
        dialog.present(self.win)
        return False

    def _answer(self, device_id: str, allow: bool, remember: bool, from_dialog: bool = False) -> None:
        self.engine.answer(device_id, allow, remember)
        self._close_request(device_id, from_dialog)

    def _close_request(self, device_id: str, from_dialog: bool = False) -> bool:
        self.withdraw_notification(f"join-{device_id}")
        dialog = self.dialogs.pop(device_id, None)
        if dialog and not from_dialog:
            dialog.force_close()
        return False


def main() -> int:
    background = "--background" in sys.argv
    argv = [a for a in sys.argv if a != "--background"]
    app = App(background)

    def _quit(*_a):
        app.quit_all()
        return GLib.SOURCE_REMOVE

    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except (ImportError, ValueError):
        add_signal = GLib.unix_signal_add
    add_signal(GLib.PRIORITY_DEFAULT, signal.SIGINT, _quit)
    add_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _quit)
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
