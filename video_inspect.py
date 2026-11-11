#!/usr/bin/env python3
"""video-inspect: a small GTK4 video player for replaying parts of a video.

Usage: video_inspect.py VIDEO.mp4

Segments are stored in VIDEO.sqlite next to the video (table "segments":
id, start, end, description; start/end in microseconds).

Keys:
  Space        play / pause
  Left/Right   seek -/+ 1 s        (Shift: 10 s, Ctrl: 0.1 s)
  , / .        step one frame back / forward
  I / O        set start / end marker at current position
  P / Enter    play between the markers
  L            toggle looping of the selection
  Home / End   jump to start / end marker
  C            clear markers
  [ / ]        slower / faster playback
  A            add a segment from the markers
  R            rotate the video view 90 degrees
"""
import os
import sqlite3
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gst, Gtk  # noqa: E402

Gst.init(None)

FPS = 30.0
FRAME_US = int(round(1_000_000 / FPS))
SEC = 1_000_000


def fmt(us):
    us = max(0, int(us))
    m, rem = divmod(us, 60 * SEC)
    s, rem = divmod(rem, SEC)
    return f"{m:02d}:{s:02d}.{rem // 1000:03d}"


SPEEDS = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]


class Player:
    """GStreamer playbin rendering into an appsink, with variable playback rate."""

    def __init__(self, uri, on_frame, on_eos, on_error, on_ready):
        self.on_frame, self.on_eos, self.on_error, self.on_ready = on_frame, on_eos, on_error, on_ready
        self.rate = 1.0
        self.playing = False
        self.ended = False
        self.duration = 0
        self.texture = None
        self._pending = None
        self._scheduled = False
        self._lock = threading.Lock()
        self.pipe = Gst.ElementFactory.make("playbin", "play")
        self.pipe.set_property("uri", uri)
        # Downscale in the pipeline: full-res RGBA frames are too heavy for Python.
        bin_ = Gst.parse_bin_from_description(
            "videoconvert ! videoscale ! "
            "video/x-raw,format=RGBA,width=1280,height=720 ! appsink name=vsink", True)
        sink = bin_.get_by_name("vsink")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 1)
        sink.set_property("sync", True)
        sink.connect("new-sample", self._sample, "pull-sample")
        sink.connect("new-preroll", self._sample, "pull-preroll")
        self.pipe.set_property("video-sink", bin_)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._eos)
        bus.connect("message::error", self._error)
        bus.connect("message::async-done", self._done)
        self.pipe.set_state(Gst.State.PAUSED)

    def _sample(self, sink, which):
        sample = sink.emit(which)
        if sample is None:
            return Gst.FlowReturn.OK
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            data = GLib.Bytes.new(bytes(info.data))
            sample.get_buffer().unmap(info)
            tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, data, w * 4)
            with self._lock:
                self._pending = tex
                if self._scheduled:
                    return Gst.FlowReturn.OK
                self._scheduled = True
            GLib.idle_add(self._set_texture)
        return Gst.FlowReturn.OK

    def _set_texture(self):
        with self._lock:
            self.texture, self._pending = self._pending, None
            self._scheduled = False
        self.on_frame()
        return False

    def _eos(self, *_):
        self.ended = True
        self.playing = False
        self.pipe.set_state(Gst.State.PAUSED)
        self.on_eos()

    def _error(self, bus, msg):
        self.on_error(msg.parse_error()[0].message)

    def _done(self, *_):
        if not self.duration:
            ok, d = self.pipe.query_duration(Gst.Format.TIME)
            if ok:
                self.duration = d // 1000
                self.on_ready()

    def get_position(self):
        ok, p = self.pipe.query_position(Gst.Format.TIME)
        return p // 1000 if ok else None

    def seek(self, us, rate=None):
        if rate is not None:
            self.rate = rate
        self.ended = False
        self.pipe.seek(self.rate, Gst.Format.TIME,
                       Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                       Gst.SeekType.SET, int(us) * 1000, Gst.SeekType.NONE, 0)

    def play(self):
        self.playing = True
        self.pipe.set_state(Gst.State.PLAYING)

    def pause(self):
        self.playing = False
        self.pipe.set_state(Gst.State.PAUSED)

    def set_rate(self, rate):
        pos = self.get_position()
        self.seek(pos or 0, rate)


class VideoView(Gtk.Widget):
    """Draws the player's latest frame letterboxed and rotated (metadata is ignored)."""

    def __init__(self, player, rotation=90):
        super().__init__()
        self.player = player
        self.rotation = rotation
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(320, 240)

    def rotate(self):
        self.rotation = (self.rotation + 90) % 360
        self.queue_draw()

    def do_snapshot(self, snap):
        tex = self.player.texture
        if tex is None:
            return
        W, H = self.get_width(), self.get_height()
        vw, vh = tex.get_width(), tex.get_height()
        dw, dh = (vh, vw) if self.rotation % 180 else (vw, vh)
        sc = min(W / dw, H / dh)
        p = Graphene.Point()
        snap.save()
        p.init(W / 2, H / 2)
        snap.translate(p)
        snap.rotate(self.rotation)
        p.init(-vw * sc / 2, -vh * sc / 2)
        snap.translate(p)
        r = Graphene.Rect()
        r.init(0, 0, vw * sc, vh * sc)
        snap.append_texture(tex, r)
        snap.restore()


class Timeline(Gtk.Widget):
    """Seekable timeline that also shows the start/end markers."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_size_request(-1, 48)
        self.set_hexpand(True)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_begin)
        drag.connect("drag-update", self.on_update)
        self._drag_x0 = 0
        self.add_controller(drag)

    def _pos_from_x(self, x):
        w = max(1, self.get_width())
        return int(min(1.0, max(0.0, x / w)) * self.app.duration)

    def on_begin(self, gesture, x, y):
        self._drag_x0 = x
        self.app.seek(self._pos_from_x(x), user=True)

    def on_update(self, gesture, dx, dy):
        self.app.seek(self._pos_from_x(self._drag_x0 + dx), user=True)

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        dur = self.app.duration or 1

        def rect(x, y, rw, rh, rgba):
            c = Gdk.RGBA()
            c.red, c.green, c.blue, c.alpha = rgba
            r = Graphene.Rect()
            r.init(x, y, max(rw, 0), rh)
            snap.append_color(c, r)

        ty, th = h / 2 - 6, 12
        fg = self.get_color()
        rect(0, ty, w, th, (fg.red, fg.green, fg.blue, 0.15))

        a, b = self.app.start, self.app.end
        if a is not None and b is not None and b > a:
            rect(a / dur * w, ty, (b - a) / dur * w, th, (0.2, 0.6, 1.0, 0.45))
        for m, col in ((a, (0.2, 0.8, 0.3, 1)), (b, (1.0, 0.45, 0.2, 1))):
            if m is not None:
                rect(m / dur * w - 1.5, 2, 3, h - 4, col)
        rect(self.app.position / dur * w - 1, 0, 2, h, (0.95, 0.15, 0.15, 1))


class SegmentDB:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS segments ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'start INTEGER NOT NULL, "end" INTEGER NOT NULL, '
            "description TEXT NOT NULL DEFAULT '')")
        self.db.commit()

    def all(self):
        return self.db.execute(
            'SELECT id, start, "end", description FROM segments ORDER BY start, id').fetchall()

    def add(self, start, end, description=""):
        cur = self.db.execute(
            'INSERT INTO segments (start, "end", description) VALUES (?, ?, ?)',
            (int(start), int(end), description))
        self.db.commit()
        return cur.lastrowid

    def set_times(self, sid, start, end):
        self.db.execute('UPDATE segments SET start=?, "end"=? WHERE id=?',
                        (int(start), int(end), sid))
        self.db.commit()

    def set_description(self, sid, text):
        self.db.execute("UPDATE segments SET description=? WHERE id=?", (text, sid))
        self.db.commit()

    def delete(self, sid):
        self.db.execute("DELETE FROM segments WHERE id=?", (sid,))
        self.db.commit()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="nl.local.VideoInspect",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.path = None
        self.media = None
        self.duration = 0
        self.start = None
        self.end = None
        self.position = 0
        self.playing_selection = False
        self.ignore_until = 0  # monotonic time (us) until which we ignore end checks

    # -- application lifecycle --------------------------------------------
    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if len(args) != 1:
            cmdline.printerr("usage: video_inspect.py VIDEO.mp4\n")
            return 2
        self.path = cmdline.create_file_for_arg(args[0])
        self.activate()
        return 0

    def do_activate(self):
        if self.path is None:
            return
        if getattr(self, "win", None):
            self.win.present()
            return
        self.build_ui()

    # -- UI ----------------------------------------------------------------
    def build_ui(self):
        css = Gtk.CssProvider()
        css.load_from_string(
            "entry.seg-entry { background: none; border: none; box-shadow: none; outline: none;"
            " min-height: 0; padding: 2px 2px; }"
            "entry.seg-entry:focus-within { background: alpha(currentColor, 0.08); }"
            "row button.flat { min-height: 0; min-width: 0; padding: 0 6px; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.win = Gtk.ApplicationWindow(application=self, title="Video Inspect")
        self.win.set_default_size(1450, 760)

        self.media = Player(self.path.get_uri(), self.on_frame, self.on_ended,
                            lambda m: print("media error:", m, file=sys.stderr),
                            self.on_prepared)
        self.picture = VideoView(self.media)
        frame = Gtk.Frame(child=self.picture)
        frame.set_vexpand(True)

        self.timeline = Timeline(self)
        self.pos_label = Gtk.Label(label="00:00.000 / 00:00.000", xalign=0)
        self.pos_label.add_css_class("monospace")
        self.sel_label = Gtk.Label(label="", xalign=1, hexpand=True)
        self.sel_label.add_css_class("monospace")
        info = Gtk.Box(spacing=12)
        info.append(self.pos_label)
        info.append(self.sel_label)

        def btn(label, cb, tip=None):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda *_: cb())
            if tip:
                b.set_tooltip_text(tip)
            return b

        self.play_btn = btn("▶ Play", self.toggle_play, "Space")
        controls = Gtk.Box(spacing=6)
        controls.append(btn("⏮ -1s", lambda: self.seek_rel(-SEC), "Left"))
        controls.append(btn("◀| frame", lambda: self.seek_rel(-FRAME_US), ","))
        controls.append(self.play_btn)
        controls.append(btn("frame |▶", lambda: self.seek_rel(FRAME_US), "."))
        controls.append(btn("+1s ⏭", lambda: self.seek_rel(SEC), "Right"))
        controls.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        controls.append(btn("[ Set start", self.set_start, "I"))
        controls.append(btn("Set end ]", self.set_end, "O"))
        controls.append(btn("Go to start", self.goto_start, "Home"))
        controls.append(btn("Go to end", self.goto_end, "End"))
        controls.append(btn("Clear", self.clear_markers, "C"))
        controls.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        controls.append(btn("▶ Play selection", self.play_selection, "P / Enter"))
        self.loop_chk = Gtk.CheckButton(label="Loop")
        self.loop_chk.set_active(True)
        controls.append(self.loop_chk)
        controls.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        controls.append(Gtk.Label(label="Speed"))
        self.speed_dd = Gtk.DropDown.new_from_strings([f"{r:g}x" for r in SPEEDS])
        self.speed_dd.set_selected(SPEEDS.index(1.0))
        self.speed_dd.set_tooltip_text("[ slower, ] faster")
        self.speed_dd.connect("notify::selected", self.on_speed_selected)
        controls.append(self.speed_dd)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(8)
        box.append(frame)
        box.append(self.timeline)
        box.append(info)
        box.append(controls)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(box)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(False)
        paned.set_end_child(self.build_segment_panel())
        paned.set_resize_end_child(False)
        paned.set_shrink_end_child(False)
        self.win.set_child(paned)
        self.reload_segments()

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.win.add_controller(keys)

        self.win.present()
        GLib.timeout_add(30, self.tick)

    # -- segments ----------------------------------------------------------
    def build_segment_panel(self):
        db_path = os.path.splitext(self.path.get_path())[0] + ".sqlite"
        self.segdb = SegmentDB(db_path)

        title = Gtk.Label(label="Segments", xalign=0)
        title.add_css_class("heading")
        dbl = Gtk.Label(label=os.path.basename(db_path), xalign=0)
        dbl.add_css_class("dim-label")
        dbl.set_ellipsize(3)
        self.seg_list = Gtk.ListBox()
        self.seg_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.seg_list.connect("row-activated", self.on_segment_activated)
        sw = Gtk.ScrolledWindow(child=self.seg_list, vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame = Gtk.Frame(child=sw)
        frame.set_vexpand(True)
        self.add_btn = Gtk.Button(label="➕ Add segment from markers")
        self.add_btn.set_tooltip_text("A  (needs a start and an end marker)")
        self.add_btn.connect("clicked", lambda *_: self.add_segment())

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.set_size_request(340, -1)
        for m in ("top", "bottom", "start", "end"):
            getattr(panel, f"set_margin_{m}")(8)
        panel.append(title)
        panel.append(dbl)
        panel.append(frame)
        panel.append(self.add_btn)
        return panel

    def reload_segments(self, select_id=None, focus=False):
        while (row := self.seg_list.get_first_child()) is not None:
            self.seg_list.remove(row)
        for sid, a, b, desc in self.segdb.all():
            row = self.make_segment_row(sid, a, b, desc)
            self.seg_list.append(row)
            if sid == select_id:
                self.seg_list.select_row(row)
                if focus:
                    row.entry.grab_focus()

    def make_segment_row(self, sid, a, b, desc):
        row = Gtk.ListBoxRow()
        row.seg = (sid, a, b)

        times = Gtk.Label(label=f"{fmt(a)} – {fmt(b)}  ({(b - a) / SEC:.2f}s)", xalign=0, hexpand=True)
        times.add_css_class("monospace")

        def ibtn(label, tip, cb):
            bt = Gtk.Button(label=label)
            bt.add_css_class("flat")
            bt.set_tooltip_text(tip)
            bt.connect("clicked", lambda *_: cb())
            return bt

        top = Gtk.Box(spacing=2)
        top.append(times)
        top.append(ibtn("▶", "Play this segment", lambda: self.play_segment(row.seg)))
        top.append(ibtn("⟲", "Replace times with current markers",
                        lambda: self.replace_segment(row.seg)))
        top.append(ibtn("🗑", "Delete segment", lambda: self.delete_segment(row.seg)))
        # Times and controls are only shown while hovering (or editing the text).
        rev = Gtk.Revealer(child=top)
        rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        rev.set_transition_duration(80)

        entry = Gtk.Entry(text=desc, placeholder_text="(no description)")
        entry.add_css_class("seg-entry")
        entry.connect("changed", lambda e: self.segdb.set_description(sid, e.get_text()))
        entry.connect("activate", lambda e: self.win.set_focus(None))
        row.entry = entry

        state = {"hover": False, "focus": False}

        def update():
            rev.set_reveal_child(state["hover"] or state["focus"])

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: (state.update(hover=True), update()))
        motion.connect("leave", lambda *_: (state.update(hover=False), update()))
        row.add_controller(motion)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: (state.update(focus=True), update()))
        focus.connect("leave", lambda *_: (state.update(focus=False), update()))
        entry.add_controller(focus)

        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for m, v in (("top", 2), ("bottom", 2), ("start", 6), ("end", 4)):
            getattr(vb, f"set_margin_{m}")(v)
        vb.append(rev)
        vb.append(entry)
        row.set_child(vb)
        return row

    def load_segment(self, seg):
        _, a, b = seg
        self.start, self.end = a, b
        self.seek(a, user=True)

    def on_segment_activated(self, lb, row):
        self.load_segment(row.seg)

    def play_segment(self, seg):
        self.load_segment(seg)
        self.play_selection()

    def can_add(self):
        return self.start is not None and self.end is not None and self.end > self.start

    def add_segment(self):
        if not self.can_add():
            return
        sid = self.segdb.add(self.start, self.end)
        self.reload_segments(select_id=sid, focus=True)

    def replace_segment(self, seg):
        if not self.can_add():
            return
        self.segdb.set_times(seg[0], self.start, self.end)
        self.reload_segments(select_id=seg[0])

    def delete_segment(self, seg):
        self.segdb.delete(seg[0])
        self.reload_segments()

    # -- media callbacks ---------------------------------------------------
    def on_prepared(self):
        self.duration = self.media.duration
        self.refresh()

    def on_frame(self):
        self.picture.queue_draw()

    def tick(self):
        pos = self.media.get_position()
        if pos is not None:
            self.position = pos
            self.check_selection()
        self.refresh()
        return True

    def check_selection(self):
        if (self.playing_selection and self.media.playing
                and GLib.get_monotonic_time() > self.ignore_until
                and self.end is not None and self.position >= self.end):
            if self.loop_chk.get_active():
                self.seek(self.start)
            else:
                self.media.pause()
                self.playing_selection = False
                self.seek(self.end)
        self.refresh()

    def on_ended(self):
        if self.playing_selection:
            if self.loop_chk.get_active():
                self.seek(self.start)
                self.media.play()
            else:
                self.playing_selection = False

    # -- actions -----------------------------------------------------------
    def refresh(self):
        self.pos_label.set_text(f"{fmt(self.position)} / {fmt(self.duration)}"
                                f"  (frame {int(round(self.position / FRAME_US))})")
        s = fmt(self.start) if self.start is not None else "--"
        e = fmt(self.end) if self.end is not None else "--"
        extra = ""
        if self.start is not None and self.end is not None:
            extra = f"  len {fmt(self.end - self.start)}"
        self.sel_label.set_text(f"start {s}   end {e}{extra}")
        self.play_btn.set_label("⏸ Pause" if self.media.playing else "▶ Play")
        self.add_btn.set_sensitive(self.can_add())
        self.timeline.queue_draw()

    def on_speed_selected(self, dd, _p):
        rate = SPEEDS[dd.get_selected()]
        if rate != self.media.rate:
            self.media.set_rate(rate)

    def change_speed(self, d):
        i = min(max(self.speed_dd.get_selected() + d, 0), len(SPEEDS) - 1)
        self.speed_dd.set_selected(i)

    def seek(self, us, user=False):
        us = int(min(max(0, us), max(0, self.duration - 1)))
        if user:
            self.playing_selection = False
        self.ignore_until = GLib.get_monotonic_time() + 150_000
        self.position = us
        self.media.seek(us)
        self.refresh()

    def seek_rel(self, delta):
        self.seek(self.position + delta, user=True)

    def toggle_play(self):
        if self.media.playing:
            self.media.pause()
            self.playing_selection = False
        else:
            if self.media.ended:
                self.seek(0)
            self.media.play()

    def set_start(self):
        self.start = self.position
        if self.end is not None and self.end <= self.start:
            self.end = None
        self.refresh()

    def set_end(self):
        self.end = self.position
        if self.start is not None and self.start >= self.end:
            self.start = None
        self.refresh()

    def clear_markers(self):
        self.start = self.end = None
        self.playing_selection = False
        self.refresh()

    def goto_start(self):
        if self.start is not None:
            self.seek(self.start, user=True)

    def goto_end(self):
        if self.end is not None:
            self.seek(self.end, user=True)

    def play_selection(self):
        start = self.start if self.start is not None else 0
        if self.end is None:
            self.end = self.duration
        self.start = start
        self.seek(start)
        self.playing_selection = True
        self.media.play()

    def on_key(self, ctrl, keyval, keycode, state):
        if isinstance(self.win.get_focus(), Gtk.Editable):
            return False  # typing a description: don't treat keys as shortcuts
        shift = state & Gdk.ModifierType.SHIFT_MASK
        ctl = state & Gdk.ModifierType.CONTROL_MASK
        step = 10 * SEC if shift else (SEC // 10 if ctl else SEC)
        k = keyval
        if k == Gdk.KEY_space:
            self.toggle_play()
        elif k == Gdk.KEY_Left:
            self.seek_rel(-step)
        elif k == Gdk.KEY_Right:
            self.seek_rel(step)
        elif k in (Gdk.KEY_comma, Gdk.KEY_less):
            self.seek_rel(-FRAME_US)
        elif k in (Gdk.KEY_period, Gdk.KEY_greater):
            self.seek_rel(FRAME_US)
        elif k in (Gdk.KEY_i, Gdk.KEY_I):
            self.set_start()
        elif k in (Gdk.KEY_o, Gdk.KEY_O):
            self.set_end()
        elif k in (Gdk.KEY_p, Gdk.KEY_P, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.play_selection()
        elif k in (Gdk.KEY_l, Gdk.KEY_L):
            self.loop_chk.set_active(not self.loop_chk.get_active())
        elif k == Gdk.KEY_Home:
            self.goto_start()
        elif k == Gdk.KEY_End:
            self.goto_end()
        elif k in (Gdk.KEY_a, Gdk.KEY_A):
            self.add_segment()
        elif k == Gdk.KEY_bracketleft:
            self.change_speed(-1)
        elif k == Gdk.KEY_bracketright:
            self.change_speed(1)
        elif k in (Gdk.KEY_r, Gdk.KEY_R):
            self.picture.rotate()
        elif k in (Gdk.KEY_c, Gdk.KEY_C):
            self.clear_markers()
        else:
            return False
        return True


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
