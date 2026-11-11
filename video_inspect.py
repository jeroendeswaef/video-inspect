#!/usr/bin/env python3
"""video-inspect: a small GTK4 video player for replaying parts of a video.

Usage: video_inspect.py VIDEO.mp4

Segments and markers are stored in VIDEO.sqlite next to the video (tables
"segments": id, start, end, description and "markers": id, time, description;
times in microseconds).

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
  M            add a marker (bookmark) at the current position (also in the menu)
  A            add a segment from the markers
  (list)       click a segment, Shift+click to select consecutive segments;
               P loops the combined time range
  R            rotate the video view 90 degrees
"""
import os
import re
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


DIAGRAM_COLORS = ["#7e57c2", "#ef8a3c", "#26a69a", "#5c8de0", "#d6568a",
                  "#8bb23a", "#b5882b", "#4f6d7a"]
def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


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
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self.on_tooltip)

    def _pos_from_x(self, x):
        w = max(1, self.get_width())
        return int(min(1.0, max(0.0, x / w)) * self.app.duration)

    def marker_at(self, x, y):
        """Marker (id, time, description) whose arrow is under (x, y), or None."""
        if y > 10:
            return None
        w, dur = max(1, self.get_width()), self.app.duration or 1
        for m in self.app.markers:
            if abs(m[1] / dur * w - x) <= 6:
                return m
        return None

    def on_tooltip(self, widget, x, y, keyboard, tooltip):
        m = self.marker_at(x, y)
        if m:
            tooltip.set_text(f"Marker {fmt(m[1])}\n{m[2]}" if m[2] else f"Marker {fmt(m[1])}")
            return True
        if not (9 <= y <= 17):
            return False
        t = self._pos_from_x(x)
        for _, a, b, desc in self.app.segments:
            if a <= t < b:
                tooltip.set_text(f"{fmt(a)} – {fmt(b)}\n{desc}" if desc else f"{fmt(a)} – {fmt(b)}")
                return True
        return False

    def on_begin(self, gesture, x, y):
        self._drag_x0 = x
        self._on_marker = self.marker_at(x, y)
        if self._on_marker:
            self.app.open_marker_dialog(self._on_marker)
            return
        self.app.seek(self._pos_from_x(x), user=True)

    def on_update(self, gesture, dx, dy):
        if getattr(self, "_on_marker", None):
            return
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

        # stored segments: a lane above the track, coloured by description
        for _, sa, sb, sd in self.app.segments:
            idx = self.app.color_of.get(norm_desc(sd))
            col = (*hex_rgb(DIAGRAM_COLORS[idx]), 0.95) if idx is not None else (0.5, 0.5, 0.5, 0.8)
            rect(sa / dur * w, 9, max((sb - sa) / dur * w - 1, 1), 8, col)

        a, b = self.app.start, self.app.end
        if a is not None and b is not None and b > a:
            rect(a / dur * w, ty, (b - a) / dur * w, th, (0.2, 0.6, 1.0, 0.45))
        for m, col in ((a, (0.2, 0.8, 0.3, 1)), (b, (1.0, 0.45, 0.2, 1))):
            if m is not None:
                rect(m / dur * w - 1.5, 2, 3, h - 4, col)
        rect(self.app.position / dur * w - 1, 0, 2, h, (0.95, 0.15, 0.15, 1))

        # bookmarks: small arrows along the top edge
        for _, mt, _d in self.app.markers:
            x = mt / dur * w
            pb = Gsk.PathBuilder.new()
            pb.move_to(x - 5, 0)
            pb.line_to(x + 5, 0)
            pb.line_to(x, 8)
            pb.close()
            c = Gdk.RGBA()
            c.red, c.green, c.blue, c.alpha = 0.85, 0.15, 0.45, 1
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, c)


def norm_desc(desc):
    """Key used to decide which segments are 'the same' (grouping and colours).

    Anything in [square brackets] is a variation and is ignored, so
    "apple jacks [back]" and "apple jacks" are the same thing.
    """
    d = " ".join(re.sub(r"\[[^\]]*\]", " ", desc or "").split()).lower()
    return d or None


def display_desc(descs):
    """Description shown for a group of 'same' segments: prefer one without a [variation]."""
    for d in descs:
        if d and d.strip() and not re.search(r"\[[^\]]*\]", d):
            return d
    return descs[0]


def find_structure(tokens):
    """Split a token sequence into single segments and repeated series.

    Returns ("seg", i) or ("group", i, length, count) nodes: a group is
    tokens[i:i+length] repeated `count` (>= 2) times back to back. At each
    position the repetition covering the most segments wins (shorter pattern
    on ties). Segments without a description (None) never form a repetition.
    """
    n, out, i = len(tokens), [], 0
    while i < n:
        best = None
        if tokens[i] is not None:
            for length in range(1, (n - i) // 2 + 1):
                pat = tokens[i:i + length]
                if None in pat:
                    break
                k = 1
                while tokens[i + k * length:i + (k + 1) * length] == pat:
                    k += 1
                if k >= 2 and (best is None or length * k > best[0] * best[1]):
                    best = (length, k)
        if best:
            out.append(("group", i, best[0], best[1]))
            i += best[0] * best[1]
        else:
            out.append(("seg", i))
            i += 1
    return out


class SegmentDB:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute(
            'CREATE TABLE IF NOT EXISTS segments ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'start INTEGER NOT NULL, "end" INTEGER NOT NULL, '
            "description TEXT NOT NULL DEFAULT '')")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS markers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "time INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '')")
        # Enforce "segments never overlap" in the database itself. Boundaries are
        # exclusive: a segment may start at the exact time another one ends (touching is ok).
        # (Triggers are recreated so rule changes also apply to existing databases.)
        for name, event, other in (
                ("segments_no_overlap_insert", "INSERT", ""),
                ("segments_no_overlap_update", 'UPDATE OF start, "end"', "AND id != NEW.id")):
            self.db.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.db.execute(
                f"CREATE TRIGGER {name} BEFORE {event} ON segments BEGIN "
                'SELECT CASE '
                'WHEN NEW."end" <= NEW.start THEN RAISE(ABORT, \'segment end must be after start\') '
                f'WHEN EXISTS (SELECT 1 FROM segments WHERE start < NEW."end" AND "end" > NEW.start {other}) '
                "THEN RAISE(ABORT, 'segment overlaps an existing segment') "
                "END; END")
        self.db.commit()

    def markers(self):
        return self.db.execute("SELECT id, time, description FROM markers ORDER BY time, id").fetchall()

    def add_marker(self, time, description=""):
        cur = self.db.execute("INSERT INTO markers (time, description) VALUES (?, ?)",
                              (int(time), description))
        self.db.commit()
        return cur.lastrowid

    def set_marker_description(self, mid, text):
        self.db.execute("UPDATE markers SET description=? WHERE id=?", (text, mid))
        self.db.commit()

    def delete_marker(self, mid):
        self.db.execute("DELETE FROM markers WHERE id=?", (mid,))
        self.db.commit()

    def find_overlap(self, start, end, exclude_id=None):
        """First existing segment (id, start, end, description) overlapping [start, end)."""
        return self.db.execute(
            'SELECT id, start, "end", description FROM segments '
            'WHERE start < ? AND "end" > ? AND id IS NOT ? ORDER BY start LIMIT 1',
            (int(end), int(start), exclude_id)).fetchone()

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
        self.segments = []
        self.markers = []
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
            "row button.flat { min-height: 0; min-width: 0; padding: 0 6px; }"
            "button.seg-circle { border-radius: 999px; min-width: 38px; min-height: 38px; padding: 0;"
            " color: white; font-weight: bold; border: none; box-shadow: none; background-image: none; }"
            "button.seg-circle.active { box-shadow: 0 0 0 3px #e53935; }"
            + "".join(f"button.seg-circle.c{i} {{ background-color: {c}; }}"
                      for i, c in enumerate(DIAGRAM_COLORS))
            + "".join(f"row.seg-row.rc{i} {{ background-color: alpha({c}, 0.6); }}"
                      for i, c in enumerate(DIAGRAM_COLORS)) +
            "row.seg-row { border-bottom: 1px solid white; }"
            "row.seg-row.picked { border-right: 5px solid #1e88e5; }"
            "row.seg-row.current { box-shadow: inset 5px 0 0 #e53935;"
            " background-image: linear-gradient(rgba(229,57,53,0.22), rgba(229,57,53,0.22)); }"
            "row.seg-row.current entry.seg-entry { font-weight: bold; }"
            "box.seg-group { border: 2px solid alpha(currentColor, 0.35); border-radius: 16px;"
            " padding: 2px 8px 4px 8px; }"
            "label.seg-group-title { font-weight: bold; opacity: 0.7; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.win = Gtk.ApplicationWindow(application=self, title="Video Inspect")
        self.win.set_default_size(1650, 760)

        header = Gtk.HeaderBar()
        menu = Gio.Menu()
        menu.append("Add marker at current position  (M)", "win.add-marker")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(mb)
        self.win.set_titlebar(header)
        act = Gio.SimpleAction.new("add-marker", None)
        act.connect("activate", lambda *_: self.add_marker())
        self.win.add_action(act)

        self.media = Player(self.path.get_uri(), self.on_frame, self.on_ended,
                            lambda m: print("media error:", m, file=sys.stderr),
                            self.on_prepared)
        self.picture = VideoView(self.media)
        frame = Gtk.Frame(child=self.picture)
        frame.set_vexpand(True)

        self.timeline = Timeline(self)
        # fixed widths so the layout (and window size) don't change while playing
        self.pos_label = Gtk.Label(label="00:00.000 / 00:00.000", xalign=0, width_chars=36)
        self.pos_label.add_css_class("monospace")
        self.sel_label = Gtk.Label(label="", xalign=1, hexpand=True, width_chars=46)
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
        def group(*widgets):
            g = Gtk.Box(spacing=6)
            for w in widgets:
                g.append(w)
            return g

        self.loop_chk = Gtk.CheckButton(label="Loop")
        self.loop_chk.set_active(True)
        self.speed_dd = Gtk.DropDown.new_from_strings([f"{r:g}x" for r in SPEEDS])
        self.speed_dd.set_selected(SPEEDS.index(1.0))
        self.speed_dd.set_tooltip_text("[ slower, ] faster")
        self.speed_dd.connect("notify::selected", self.on_speed_selected)

        # The button groups wrap onto extra rows when the video column is narrow,
        # so the window's minimum width stays small.
        controls = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                               column_spacing=16, row_spacing=6, max_children_per_line=20)
        groups = [
            group(btn("⏮ -1s", lambda: self.seek_rel(-SEC), "Left"),
                  btn("◀| frame", lambda: self.seek_rel(-FRAME_US), ","),
                  self.play_btn,
                  btn("frame |▶", lambda: self.seek_rel(FRAME_US), "."),
                  btn("+1s ⏭", lambda: self.seek_rel(SEC), "Right")),
            group(btn("[ Set start", self.set_start, "I"),
                  btn("Set end ]", self.set_end, "O"),
                  btn("Go to start", self.goto_start, "Home"),
                  btn("Go to end", self.goto_end, "End"),
                  btn("Clear", self.clear_markers, "C")),
            group(btn("▶ Play selection", self.play_selection, "P / Enter"), self.loop_chk),
            group(Gtk.Label(label="Speed"), self.speed_dd),
        ]
        for i, g in enumerate(groups):
            controls.append(g)
            controls.get_child_at_index(i).set_focusable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(8)
        box.append(frame)
        box.append(self.timeline)
        box.append(info)
        box.append(controls)

        # video | sequence diagram | segment list
        inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_start_child(box)
        inner.set_resize_start_child(True)
        inner.set_shrink_start_child(False)
        inner.set_end_child(self.build_diagram())
        inner.set_resize_end_child(False)
        inner.set_shrink_end_child(False)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(inner)
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
        self.markers = self.segdb.markers()

        title = Gtk.Label(label="Segments", xalign=0)
        title.add_css_class("heading")
        dbl = Gtk.Label(label=os.path.basename(db_path), xalign=0)
        dbl.add_css_class("dim-label")
        dbl.set_ellipsize(3)
        self.seg_list = Gtk.ListBox()
        self.seg_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.seg_rows = []
        click = Gtk.GestureClick()
        click.connect("pressed", self.on_list_click)
        self.seg_list.add_controller(click)
        self.pick = None         # (first_id, last_id) of the selected consecutive segments
        self.pick_anchor = None  # id of the segment a Shift+click range starts from
        sw = self.seg_sw = Gtk.ScrolledWindow(child=self.seg_list, vexpand=True)
        sw.set_overlay_scrolling(False)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame = Gtk.Frame(child=sw)
        frame.set_vexpand(True)
        self.add_btn = Gtk.Button(label="➕ Add segment from markers")
        self.add_btn.set_tooltip_text("A  (needs a start and an end marker)")
        self.add_btn.connect("clicked", lambda *_: self.add_segment())

        self.pick_label = Gtk.Label(xalign=0, wrap=True)
        self.seg_status = Gtk.Label(label="", xalign=0, wrap=True)
        self.seg_status.add_css_class("error")
        self._conflict_key = None

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.set_size_request(340, -1)
        for m in ("top", "bottom", "start", "end"):
            getattr(panel, f"set_margin_{m}")(8)
        panel.append(title)
        panel.append(dbl)
        panel.append(frame)
        panel.append(self.pick_label)
        panel.append(self.seg_status)
        panel.append(self.add_btn)
        return panel

    def conflict_message(self, exclude_id=None):
        """Why the current markers can't be stored as a segment, or None if they can."""
        if not self.can_add():
            return None
        o = self.segdb.find_overlap(self.start, self.end, exclude_id)
        if o is None:
            return None
        return f"Markers overlap an existing segment ({fmt(o[1])} – {fmt(o[2])})"

    def update_add_state(self):
        key = (self.start, self.end)
        if key != self._conflict_key:
            self._conflict_key = key
            self._conflict = self.conflict_message()
            self.seg_status.set_text("")  # errors only appear after a failed attempt
            self.add_btn.set_tooltip_text(
                self._conflict or "A  (needs a start and an end marker)")
        self.add_btn.set_sensitive(self.can_add() and not self._conflict)

    # -- sequence diagram --------------------------------------------------
    def build_diagram(self):
        self.diagram_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                   halign=Gtk.Align.START)
        for m in ("top", "bottom", "start", "end"):
            getattr(self.diagram_box, f"set_margin_{m}")(8)
        sw = Gtk.ScrolledWindow(child=self.diagram_box)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_overlay_scrolling(False)  # keep the scrollbar visible when it overflows
        self.diagram_sw = sw

        self.circles = []
        self._active_id = None
        self.color_of = {}
        self._diagram_timer = 0
        frame = Gtk.Frame(child=sw)
        frame.set_size_request(210, -1)
        return frame

    def rebuild_diagram(self):
        self._diagram_timer = 0
        while (c := self.diagram_box.get_first_child()) is not None:
            self.diagram_box.remove(c)
        self.circles = []
        segs = self.segments
        tokens = [norm_desc(d) for _, _, _, d in segs]
        color_of = {}
        for t in tokens:
            if t is not None and t not in color_of:
                color_of[t] = len(color_of) % len(DIAGRAM_COLORS)
        self.color_of = color_of

        def arrow():
            return Gtk.Label(label="↓", halign=Gtk.Align.START, margin_start=14)

        def item(i, occ):
            vb = Gtk.Box(spacing=8)
            circle = Gtk.Button(label=str(i + 1), valign=Gtk.Align.CENTER)
            circle.add_css_class("seg-circle")
            circle.add_css_class(f"c{color_of[tokens[i]]}" if tokens[i] is not None else "c7")
            circle.occ = [segs[j] for j in occ]
            varied = len({o[3] for o in circle.occ}) > 1  # differ only by [variations]
            lines = [f"{fmt(a)} – {fmt(b)}" + (f"  #{n + 1}" if len(occ) > 1 else "")
                     + (f"  {d}" if varied else "")
                     for n, (_, a, b, d) in enumerate(circle.occ)]
            if segs[i][3] and not varied:
                lines.insert(0, segs[i][3])
            if len(occ) > 1:
                lines.append(f"repeats ×{len(occ)} – click to cycle through them")
            circle.set_tooltip_text("\n".join(lines))
            circle.connect("clicked", self.on_circle_clicked)
            self.circles.append(circle)
            cap = Gtk.Label(label=display_desc([o[3] for o in circle.occ]) or "–", max_width_chars=16, ellipsize=3,
                            xalign=0, hexpand=True)
            cap.add_css_class("caption")
            vb.append(circle)
            vb.append(cap)
            return vb

        for n, node in enumerate(find_structure(tokens)):
            if n:
                self.diagram_box.append(arrow())
            if node[0] == "seg":
                self.diagram_box.append(item(node[1], [node[1]]))
                continue
            _, i, length, count = node
            grp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            grp.add_css_class("seg-group")
            title = Gtk.Label(label=f"↻ ×{count}", xalign=0)
            title.add_css_class("seg-group-title")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for j in range(length):
                if j:
                    inner.append(arrow())
                inner.append(item(i + j, [i + r * length + j for r in range(count)]))
            grp.append(title)
            grp.append(inner)
            self.diagram_box.append(grp)
        self._active_id = None
        self.update_active()
        self.recolor_rows()

    def recolor_rows(self):
        """Give each segment row the colour of its description (as in the diagram)."""
        desc = {sid: d for sid, _a, _b, d in self.segments}
        for row in self.seg_rows:
            t = norm_desc(desc.get(row.seg[0]))
            cls = f"rc{self.color_of[t]}" if t in self.color_of else None
            if cls != row.color_cls:
                if row.color_cls:
                    row.remove_css_class(row.color_cls)
                if cls:
                    row.add_css_class(cls)
                row.color_cls = cls

    def schedule_diagram_rebuild(self):
        if self._diagram_timer:
            GLib.source_remove(self._diagram_timer)
        self._diagram_timer = GLib.timeout_add(400, lambda: self.rebuild_diagram() or False)

    def on_circle_clicked(self, circle):
        ids = [o[0] for o in circle.occ]
        k = (ids.index(self._active_id) + 1) % len(ids) if self._active_id in ids else 0
        sid, a, b, _d = circle.occ[k]
        self.load_segment((sid, a, b))

    def update_active(self):
        active = next((sid for sid, a, b, _d in self.segments if a <= self.position < b), None)
        if active == self._active_id:
            return
        self._active_id = active
        for c in self.circles:
            if any(o[0] == active for o in c.occ):
                c.add_css_class("active")
                self.scroll_into_view(c, self.diagram_box, self.diagram_sw)
            else:
                c.remove_css_class("active")
        for row in self.seg_rows:
            if row.seg[0] == active:
                row.add_css_class("current")
                self.scroll_row_into_view(row)
            else:
                row.remove_css_class("current")

    def scroll_row_into_view(self, row):
        self.scroll_into_view(row, self.seg_list, self.seg_sw)

    @staticmethod
    def scroll_into_view(widget, container, sw):
        ok, r = widget.compute_bounds(container)
        if not ok:
            return
        adj = sw.get_vadjustment()
        if r.get_y() < adj.get_value() or r.get_y() + r.get_height() > adj.get_value() + adj.get_page_size():
            adj.set_value(max(0, r.get_y() - 20))

    def reload_segments(self, select_id=None, focus=False):
        self._conflict_key = None
        self.segments = self.segdb.all()
        self.seg_rows = []
        self.rebuild_diagram()
        self.timeline.queue_draw()
        while (row := self.seg_list.get_first_child()) is not None:
            self.seg_list.remove(row)
        for sid, a, b, desc in self.segments:
            row = self.make_segment_row(sid, a, b, desc)
            self.seg_list.append(row)
            self.seg_rows.append(row)
            if sid == select_id and focus:
                row.entry.grab_focus()
        self.recolor_rows()
        self._active_id = None
        self.update_active()
        self.apply_pick()

    def make_segment_row(self, sid, a, b, desc):
        row = Gtk.ListBoxRow()
        row.add_css_class("seg-row")
        row.color_cls = None
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
        entry.connect("changed", lambda e: self.on_description_changed(sid, e.get_text()))
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

    def on_description_changed(self, sid, text):
        self.segdb.set_description(sid, text)
        self.segments = [(i, a, b, text if i == sid else d) for i, a, b, d in self.segments]
        self.schedule_diagram_rebuild()

    def load_segment(self, seg):
        _, a, b = seg
        self.start, self.end = a, b
        self.seek(a, user=True)

    # -- markers (bookmarks) -------------------------------------------------
    def add_marker(self):
        self.open_marker_dialog(None, self.position)

    def open_marker_dialog(self, marker, time=None):
        """Add a marker at `time` (marker is None) or edit/delete an existing one."""
        if marker:
            time = marker[1]
        dlg = Gtk.Window(transient_for=self.win, modal=True, resizable=False,
                         title="Edit marker" if marker else "Add marker")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        pos = Gtk.Label(label=f"Position {fmt(time)}", xalign=0)
        pos.add_css_class("monospace")
        entry = Gtk.Entry(text=marker[2] if marker else "", placeholder_text="description",
                          width_chars=38)

        def close():
            dlg.close()

        def save():
            text = entry.get_text().strip()
            if marker:
                self.segdb.set_marker_description(marker[0], text)
            else:
                self.segdb.add_marker(time, text)
            self.reload_markers()
            close()

        def delete():
            self.segdb.delete_marker(marker[0])
            self.reload_markers()
            close()

        def goto():
            self.seek(time, user=True)
            close()

        def button(label, cb, css=None):
            b = Gtk.Button(label=label)
            if css:
                b.add_css_class(css)
            b.connect("clicked", lambda *_: cb())
            return b

        row = Gtk.Box(spacing=6)
        if marker:
            row.append(button("Delete", delete, "destructive-action"))
            row.append(button("Go to", goto))
        row.append(Gtk.Box(hexpand=True))
        row.append(button("Cancel", close))
        row.append(button("Save", save, "suggested-action"))
        box.append(pos)
        box.append(entry)
        box.append(row)
        dlg.set_child(box)
        entry.connect("activate", lambda *_: save())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed",
                     lambda c, kv, kc, st: (close(), True)[1] if kv == Gdk.KEY_Escape else False)
        dlg.add_controller(keys)
        dlg.present()
        entry.grab_focus()
        self._marker_dialog, self._marker_entry, self._marker_save = dlg, entry, save

    def reload_markers(self):
        self.markers = self.segdb.markers()
        self.timeline.queue_draw()

    # -- selecting consecutive segments --------------------------------------
    def seg_index(self, sid):
        return next((i for i, sg in enumerate(self.segments) if sg[0] == sid), None)

    def pick_range(self):
        """Indices (i0, i1) of the selected segments, or None."""
        if not self.pick:
            return None
        i0, i1 = self.seg_index(self.pick[0]), self.seg_index(self.pick[1])
        return None if i0 is None or i1 is None else (i0, i1)

    def pick_union(self):
        r = self.pick_range()
        return None if r is None else (self.segments[r[0]][1], self.segments[r[1]][2])

    def on_list_click(self, gesture, n_press, x, y):
        row = self.seg_list.get_row_at_y(int(y))
        if row is None:
            return
        self.win.set_focus(None)  # leave any description box so shortcuts work again
        sid = row.seg[0]
        shift = gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
        anchor = self.seg_index(self.pick_anchor) if self.pick_anchor is not None else None
        if shift and anchor is not None:
            i0, i1 = sorted((anchor, self.seg_index(sid)))
            self.start, self.end = self.segments[i0][1], self.segments[i1][2]
            self.pick = (self.segments[i0][0], self.segments[i1][0])
            self.seek(self.start, user=True)
        else:
            self.pick_anchor = sid
            self.load_segment(row.seg)
            self.pick = (sid, sid)
        self.apply_pick()

    def apply_pick(self):
        r = self.pick_range()
        if r is None:
            self.pick = None
        for i, row in enumerate(self.seg_rows):
            if r is not None and r[0] <= i <= r[1]:
                row.add_css_class("picked")
            else:
                row.remove_css_class("picked")
        if r is not None and r[1] > r[0]:
            a, b = self.pick_union()
            self.pick_label.set_text(f"{r[1] - r[0] + 1} segments selected: {fmt(a)} – {fmt(b)} "
                                     f"({(b - a) / SEC:.2f}s). Play selection (P) loops them.")
        else:
            self.pick_label.set_text("Shift+click to select several consecutive segments")

    def validate_pick(self):
        """Drop the selection as soon as the markers no longer match it."""
        if self.pick and self.pick_union() != (self.start, self.end):
            self.pick = None
            self.apply_pick()

    def play_segment(self, seg):
        r = self.pick_range()
        i = self.seg_index(seg[0])
        if r is not None and r[1] > r[0] and r[0] <= i <= r[1]:
            self.play_selection()  # markers already span the whole selection
        else:
            self.load_segment(seg)
            self.pick = (seg[0], seg[0])
            self.pick_anchor = seg[0]
            self.apply_pick()
            self.play_selection()

    def can_add(self):
        return self.start is not None and self.end is not None and self.end > self.start

    def add_segment(self):
        if not self.can_add():
            return
        msg = self.conflict_message()
        if msg:
            self.seg_status.set_text(msg)
            return
        try:
            sid = self.segdb.add(self.start, self.end)
        except sqlite3.IntegrityError as e:
            self.seg_status.set_text(str(e))
            return
        self.reload_segments(select_id=sid, focus=True)

    def replace_segment(self, seg):
        if not self.can_add():
            return
        msg = self.conflict_message(exclude_id=seg[0])
        if msg:
            self.seg_status.set_text(msg)
            return
        try:
            self.segdb.set_times(seg[0], self.start, self.end)
        except sqlite3.IntegrityError as e:
            self.seg_status.set_text(str(e))
            return
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
        self.update_add_state()
        self.update_active()
        self.validate_pick()
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
        elif k in (Gdk.KEY_m, Gdk.KEY_M):
            self.add_marker()
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
