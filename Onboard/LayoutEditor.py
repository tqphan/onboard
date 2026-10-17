# -*- coding: utf-8 -*-

# Copyright © 2026 Tom Phan <tom.quoc.phan@gmail.com>
#
# This file is part of Onboard.
#
# Onboard is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# Onboard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
Graphical editor for creating and changing Onboard keyboard layouts.
"""

import os
import sys
import math
import shutil
import subprocess
from xml.dom import minidom

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gtk, Gdk, GLib, Pango, PangoCairo

from Onboard.LayoutEditorModel import (
    EditorDocument, EditorLayer, EditorKey, KEY_UNIT, KEY_GAP, SNAP,
    TEMPLATE_KEYS, US_LABELS, QWERTY_ROWS, LAYER_KEY_PATTERN,
    MANAGED_ATTRIBUTES, layer_key, snap)
from Onboard.utils import XDGDirs

import logging
_logger = logging.getLogger(__name__)


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SIZE_GROUPS = ["alphanumeric", "bottomrow", "click", "nowordlist"]

ACTION_LABELS = [
    ("char", "Type text"),
    ("keycode", "Keycode (follows system keymap)"),
    ("template", "Special key"),
    ("layer", "Switch layer"),
    ("keysym", "Keysym"),
    ("keypress_name", "Keysym name"),
    ("macro", "Snippet"),
    ("none", "Nothing (decoration)"),
]

ACTION_HINTS = {
    "char": "Text typed by the key, e.g. “a”, “ñ” or “.com”.",
    "keycode": "X keycode, e.g. 38 for the A key. The label is taken "
               "from the active keyboard layout.",
    "keysym": "Keysym value, e.g. 0xff0d for Return.",
    "keypress_name": "Keysym name, e.g. Return or KP_Enter.",
    "macro": "Snippet number. Edit snippets in Onboard's preferences.",
    "template": "Predefined Onboard key; labels, modifiers and "
                "behavior come from key_defs.xml.",
    "layer": "Shows another layer of this keyboard.",
    "none": "The key is drawn but does nothing.",
}

TYPE_COLORS = {
    "char": (0.86, 0.86, 0.86),
    "keycode": (0.86, 0.86, 0.86),
    "template": (0.80, 0.70, 0.70),
    "layer": (0.85, 0.76, 0.55),
    "keysym": (0.75, 0.80, 0.86),
    "keypress_name": (0.75, 0.80, 0.86),
    "macro": (0.75, 0.86, 0.76),
    "none": (0.93, 0.93, 0.93),
}

HANDLE_SIZE = 8         # resize handle, in pixels
CANVAS_MARGIN = 16      # pixels around the keyboard
MIN_VIEW_UNITS = (12, 5)


def find_layouts_dir():
    """ Directory holding key_defs.xml and the system layouts. """
    for path in (os.path.join(PROJECT_DIR, "layouts"),
                 "/usr/share/onboard/layouts",
                 "/usr/local/share/onboard/layouts"):
        if os.path.exists(os.path.join(path, "key_defs.xml")):
            return path
    return None


def get_user_layouts_dir():
    return os.path.join(XDGDirs.get_data_home("onboard"), "layouts")


def load_template_keycodes(layouts_dir):
    """ {template id: keycode} from key_defs.xml. """
    result = {}
    if not layouts_dir:
        return result
    try:
        dom = minidom.parse(os.path.join(layouts_dir, "key_defs.xml"))
        for node in dom.getElementsByTagName("key_template"):
            if node.getAttribute("keycode"):
                result[node.getAttribute("id")] = \
                    int(node.getAttribute("keycode"))
    except (OSError, ValueError) as ex:
        _logger.warning("Failed to read key_defs.xml: {}".format(ex))
    return result


class KeymapLabels:
    """ Labels of keycodes in the active keyboard layout. """

    def __init__(self, template_keycodes):
        self._template_keycodes = template_keycodes
        self._cache = {}

    def get_label(self, key):
        keycode = None
        if "keycode" in key.attributes:
            try:
                keycode = int(key.attributes["keycode"])
            except ValueError:
                return None
        elif key.id in US_LABELS:
            keycode = self._template_keycodes.get(key.id)
        if keycode is None:
            return None
        if keycode not in self._cache:
            self._cache[keycode] = self._lookup(keycode) or \
                US_LABELS.get(key.id)
        return self._cache[keycode]

    @staticmethod
    def _lookup(keycode):
        try:
            keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
            ok, keys, keyvals = keymap.get_entries_for_keycode(keycode)
        except (TypeError, AttributeError):
            return None
        if not ok or not keyvals:
            return None
        c = Gdk.keyval_to_unicode(keyvals[0])
        if c and chr(c).isprintable() and not chr(c).isspace():
            return chr(c)
        return Gdk.keyval_name(keyvals[0])


class KeyboardCanvas(Gtk.DrawingArea):
    """ Shows one layer and lets the user place and resize keys. """

    def __init__(self, editor):
        Gtk.DrawingArea.__init__(self)
        self.editor = editor
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK |
                        Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_button_press)
        self.connect("button-release-event", self._on_button_release)
        self.connect("motion-notify-event", self._on_motion)
        self.set_size_request(400, 220)

        self._drag = None           # active mouse operation
        self._frozen_view = None    # keeps the scale stable while dragging

    # ------------------------------------------------------------------
    # Coordinates

    def _get_view(self):
        """ (scale, offset_x, offset_y) mapping SVG units to pixels. """
        if self._frozen_view:
            return self._frozen_view
        w, h = self.editor.doc.get_extents()
        w = max(w, MIN_VIEW_UNITS[0] * KEY_UNIT) + KEY_UNIT
        h = max(h, MIN_VIEW_UNITS[1] * KEY_UNIT) + KEY_UNIT
        aw = self.get_allocated_width() - 2 * CANVAS_MARGIN
        ah = self.get_allocated_height() - 2 * CANVAS_MARGIN
        scale = max(0.1, min(aw / w, ah / h))
        return scale, CANVAS_MARGIN, CANVAS_MARGIN

    def to_model(self, px, py):
        scale, ox, oy = self._get_view()
        return (px - ox) / scale, (py - oy) / scale

    def _in_resize_handle(self, key, px, py):
        scale, ox, oy = self._get_view()
        hx = ox + (key.x + key.w) * scale
        hy = oy + (key.y + key.h) * scale
        return abs(px - hx) <= HANDLE_SIZE and abs(py - hy) <= HANDLE_SIZE

    # ------------------------------------------------------------------
    # Drawing

    def _on_draw(self, widget, cr):
        context = self.get_style_context()
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        Gtk.render_background(context, cr, 0, 0, w, h)

        scale, ox, oy = self._get_view()
        cr.save()
        cr.translate(ox, oy)

        # background and grid
        vw, vh = (w - 2 * ox) / scale, (h - 2 * oy) / scale
        cr.set_source_rgb(0.97, 0.97, 0.97)
        cr.rectangle(0, 0, vw * scale, vh * scale)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.25)
        step = KEY_UNIT * scale
        y = 0.0
        while y <= vh * scale + 0.5:
            x = 0.0
            while x <= vw * scale + 0.5:
                cr.rectangle(x - 1, y - 1, 2, 2)
                x += step
            y += step
        cr.fill()

        # keyboard extent, the size Onboard will see
        ew, eh = self.editor.doc.get_extents()
        if ew and eh:
            cr.set_source_rgba(0.2, 0.4, 0.8, 0.5)
            cr.set_line_width(1)
            cr.set_dash([4, 3])
            cr.rectangle(0.5, 0.5, ew * scale, eh * scale)
            cr.stroke()
            cr.set_dash([])

        layer = self.editor.get_current_layer()
        selection = self.editor.selection
        layer_names = self.editor.doc.get_layer_names()
        for key in layer.keys:
            self._draw_key(cr, key, scale, key in selection, layer_names)

        # rubber band
        if self._drag and self._drag["mode"] == "band":
            x0, y0, x1, y1 = self._drag["band"]
            cr.set_source_rgba(0.2, 0.4, 0.9, 0.15)
            cr.rectangle(min(x0, x1) * scale, min(y0, y1) * scale,
                         abs(x1 - x0) * scale, abs(y1 - y0) * scale)
            cr.fill_preserve()
            cr.set_source_rgba(0.2, 0.4, 0.9, 0.8)
            cr.set_line_width(1)
            cr.stroke()

        cr.restore()

        if not layer.keys:
            self._draw_hint(cr, w, h, _("Double-click to add a key"))

    def _draw_key(self, cr, key, scale, selected, layer_names):
        x, y = key.x * scale, key.y * scale
        w, h = key.w * scale, key.h * scale
        r = min(4.0, w / 4, h / 4)

        _rounded_rect(cr, x, y, w, h, r)
        cr.set_source_rgb(*TYPE_COLORS.get(key.get_action_type(),
                                           TYPE_COLORS["none"]))
        cr.fill_preserve()
        if selected:
            cr.set_source_rgb(0.15, 0.4, 0.95)
            cr.set_line_width(2.5)
        else:
            cr.set_source_rgb(0.3, 0.3, 0.3)
            cr.set_line_width(1)
        cr.stroke()

        label = self.editor.keymap_labels.get_label(key) \
            if not key.attributes.get("label") else None
        if not label:
            label = key.get_display_label(layer_names)
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription("Sans")
        font.set_absolute_size(max(4.0, min(h * 0.45, w * 0.6)) *
                               Pango.SCALE)
        layout.set_font_description(font)
        layout.set_text(label.replace("\\n", "\n"), -1)
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_width(int(max(1, w - 4) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        lw, lh = layout.get_pixel_size()
        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.move_to(x + 2, y + (h - lh) / 2)
        PangoCairo.show_layout(cr, layout)

        if selected and len(self.editor.selection) == 1:
            cr.set_source_rgb(0.15, 0.4, 0.95)
            s = HANDLE_SIZE
            cr.rectangle(x + w - s / 2, y + h - s / 2, s, s)
            cr.fill()

    def _draw_hint(self, cr, w, h, text):
        layout = PangoCairo.create_layout(cr)
        layout.set_text(text, -1)
        lw, lh = layout.get_pixel_size()
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.move_to((w - lw) / 2, (h - lh) / 2)
        PangoCairo.show_layout(cr, layout)

    # ------------------------------------------------------------------
    # Mouse

    def _on_button_press(self, widget, event):
        self.grab_focus()
        if event.button != 1:
            return False
        editor = self.editor
        layer = editor.get_current_layer()
        mx, my = self.to_model(event.x, event.y)
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK

        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self._drag = None
            self._frozen_view = None
            key = layer.key_at(mx, my)
            if key:
                editor.focus_label_entry()
            else:
                editor.add_key_at(mx, my)
            return True

        self._frozen_view = self._get_view()

        if len(editor.selection) == 1 and \
           self._in_resize_handle(editor.selection[0], event.x, event.y):
            key = editor.selection[0]
            editor.begin_edit()
            self._drag = {"mode": "resize", "start": (mx, my),
                          "orig": (key.w, key.h)}
            return True

        key = layer.key_at(mx, my)
        if key:
            if ctrl:
                editor.toggle_selected(key)
            elif key not in editor.selection:
                editor.set_selection([key])
            if key in editor.selection:
                editor.begin_edit()
                self._drag = {"mode": "move", "start": (mx, my),
                              "anchor": key,
                              "orig": [(k, k.x, k.y)
                                       for k in editor.selection]}
        else:
            if not ctrl:
                editor.set_selection([])
            self._drag = {"mode": "band", "band": [mx, my, mx, my],
                          "base": list(editor.selection)}
        return True

    def _on_motion(self, widget, event):
        if not self._drag:
            self._update_cursor(event)
            return False
        editor = self.editor
        mx, my = self.to_model(event.x, event.y)
        mode = self._drag["mode"]
        use_snap = editor.snap_enabled()

        if mode == "move":
            sx, sy = self._drag["start"]
            dx, dy = mx - sx, my - sy
            if use_snap:
                anchor = self._drag["anchor"]
                for k, ox, oy in self._drag["orig"]:
                    if k is anchor:
                        dx = snap(ox + dx - KEY_GAP / 2) + KEY_GAP / 2 - ox
                        dy = snap(oy + dy - KEY_GAP / 2) + KEY_GAP / 2 - oy
            min_x = min(ox for k, ox, oy in self._drag["orig"])
            min_y = min(oy for k, ox, oy in self._drag["orig"])
            dx = max(dx, KEY_GAP / 2 - min_x)
            dy = max(dy, KEY_GAP / 2 - min_y)
            for k, ox, oy in self._drag["orig"]:
                k.x, k.y = ox + dx, oy + dy
            editor.on_geometry_changed()

        elif mode == "resize":
            key = editor.selection[0]
            sx, sy = self._drag["start"]
            ow, oh = self._drag["orig"]
            w, h = ow + mx - sx, oh + my - sy
            if use_snap:
                w = snap(w + KEY_GAP) - KEY_GAP
                h = snap(h + KEY_GAP) - KEY_GAP
            key.w = max(SNAP - KEY_GAP if use_snap else 2.0, w)
            key.h = max(SNAP - KEY_GAP if use_snap else 2.0, h)
            editor.on_geometry_changed()

        elif mode == "band":
            band = self._drag["band"]
            band[2], band[3] = mx, my
            x0, x1 = sorted((band[0], band[2]))
            y0, y1 = sorted((band[1], band[3]))
            inside = [k for k in editor.get_current_layer().keys
                      if k.x < x1 and k.x + k.w > x0 and
                      k.y < y1 and k.y + k.h > y0]
            base = self._drag["base"]
            editor.set_selection(base + [k for k in inside
                                         if k not in base])
            self.queue_draw()
        return True

    def _on_button_release(self, widget, event):
        if self._drag:
            self._drag = None
            self._frozen_view = None
            self.editor.end_edit()
            self.queue_draw()
        return True

    def _update_cursor(self, event):
        window = self.get_window()
        if not window:
            return
        selection = self.editor.selection
        name = "default"
        if len(selection) == 1 and \
           self._in_resize_handle(selection[0], event.x, event.y):
            name = "se-resize"
        else:
            mx, my = self.to_model(event.x, event.y)
            if self.editor.get_current_layer().key_at(mx, my):
                name = "move"
        cursor = Gdk.Cursor.new_from_name(window.get_display(), name)
        window.set_cursor(cursor)


class LayoutEditor(Gtk.Window):
    """ Main window of the layout editor. """

    def __init__(self, filename=None):
        Gtk.Window.__init__(self)
        self.set_default_size(1100, 600)
        self.set_icon_name("onboard")

        self.layouts_dir = find_layouts_dir()
        self.keymap_labels = KeymapLabels(
            load_template_keycodes(self.layouts_dir))

        self.doc = EditorDocument()
        self.current_layer = 0
        self.selection = []
        self._undo_stack = []
        self._redo_stack = []
        self._edit_tag = None
        self._edit_pending = False
        self._dirty = False
        self._updating = False      # suppresses feedback from widgets
        self._preview_process = None

        self._build_ui()
        self.connect("delete-event", self._on_delete_event)
        self.connect("key-press-event", self._on_key_press)

        if filename:
            self._load(filename)
        else:
            self._set_document(EditorDocument.new_qwerty())
        self._dirty = False
        self._update_title()

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self):
        header = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(header)
        self._header = header

        new_button = Gtk.MenuButton(label=_("New"))
        menu = Gtk.Menu()
        for label, callback in [
                (_("Typewriter keyboard (QWERTY)"), self._on_new_qwerty),
                (_("Grid of keys…"), self._on_new_grid),
                (_("Empty keyboard"), self._on_new_empty)]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        new_button.set_popup(menu)
        header.pack_start(new_button)

        open_button = Gtk.Button(label=_("Open…"))
        open_button.connect("clicked", self._on_open)
        header.pack_start(open_button)

        box = Gtk.Box(spacing=0)
        box.get_style_context().add_class("linked")
        self._undo_button = _icon_button("edit-undo-symbolic", _("Undo"),
                                         self._on_undo)
        self._redo_button = _icon_button("edit-redo-symbolic", _("Redo"),
                                         self._on_redo)
        box.pack_start(self._undo_button, False, False, 0)
        box.pack_start(self._redo_button, False, False, 0)
        header.pack_start(box)

        try_button = Gtk.Button(label=_("Try in Onboard"))
        try_button.get_style_context().add_class("suggested-action")
        try_button.set_tooltip_text(
            _("Save and open the keyboard in a separate Onboard window. "
              "Your Onboard settings stay unchanged."))
        try_button.connect("clicked", self._on_try)
        header.pack_end(try_button)

        save_as_button = Gtk.Button(label=_("Save As…"))
        save_as_button.connect("clicked", self._on_save_as)
        header.pack_end(save_as_button)
        save_button = Gtk.Button(label=_("Save"))
        save_button.connect("clicked", self._on_save)
        header.pack_end(save_button)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(paned)

        # left: toolbar, canvas, status
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin=6)
        paned.pack1(left, True, False)

        toolbar = Gtk.Box(spacing=6)
        left.pack_start(toolbar, False, False, 0)

        toolbar.pack_start(Gtk.Label(label=_("Layer:")), False, False, 0)
        self._layer_combo = Gtk.ComboBoxText()
        self._layer_combo.connect("changed", self._on_layer_changed)
        toolbar.pack_start(self._layer_combo, False, False, 0)
        layer_box = Gtk.Box()
        layer_box.get_style_context().add_class("linked")
        for icon, tip, cb in [
                ("list-add-symbolic", _("Add layer"), self._on_add_layer),
                ("document-edit-symbolic", _("Rename layer"),
                 self._on_rename_layer),
                ("edit-copy-symbolic", _("Duplicate layer"),
                 self._on_duplicate_layer),
                ("list-remove-symbolic", _("Remove layer"),
                 self._on_remove_layer)]:
            layer_box.pack_start(_icon_button(icon, tip, cb),
                                 False, False, 0)
        toolbar.pack_start(layer_box, False, False, 0)

        toolbar.pack_start(Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        add_button = Gtk.Button(label=_("Add Key"))
        add_button.connect("clicked", lambda b: self.add_key_at(None, None))
        toolbar.pack_start(add_button, False, False, 0)

        special_button = Gtk.MenuButton(label=_("Add Special Key"))
        menu = Gtk.Menu()
        for id, description in TEMPLATE_KEYS:
            item = Gtk.MenuItem(label=description)
            item.connect("activate", self._on_add_template_key, id)
            menu.append(item)
        menu.show_all()
        special_button.set_popup(menu)
        toolbar.pack_start(special_button, False, False, 0)

        edit_box = Gtk.Box()
        edit_box.get_style_context().add_class("linked")
        edit_box.pack_start(_icon_button("edit-copy-symbolic",
                                         _("Duplicate keys (Ctrl+D)"),
                                         lambda b: self.duplicate_selection()),
                            False, False, 0)
        edit_box.pack_start(_icon_button("edit-delete-symbolic",
                                         _("Delete keys (Del)"),
                                         lambda b: self.delete_selection()),
                            False, False, 0)
        toolbar.pack_start(edit_box, False, False, 0)

        self._snap_check = Gtk.CheckButton(label=_("Snap to grid"),
                                           active=True)
        self._snap_check.set_tooltip_text(
            _("Hold Shift while using arrow keys to move by a whole key"))
        toolbar.pack_end(self._snap_check, False, False, 0)

        frame = Gtk.Frame()
        self.canvas = KeyboardCanvas(self)
        frame.add(self.canvas)
        left.pack_start(frame, True, True, 0)

        self._status = Gtk.Label(xalign=0.0)
        self._status.get_style_context().add_class("dim-label")
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        left.pack_start(self._status, False, False, 0)

        # right: properties
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_size_request(340, -1)
        paned.pack2(scrolled, False, False)
        props = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin=12)
        scrolled.add(props)

        props.pack_start(_heading(_("Keyboard")), False, False, 0)
        grid = _form_grid()
        props.pack_start(grid, False, False, 0)
        self._name_entry = Gtk.Entry()
        self._name_entry.set_tooltip_text(
            _("Also used as the file name of the layout"))
        self._name_entry.connect("changed", self._on_doc_property_changed)
        _form_row(grid, 0, _("Name"), self._name_entry)
        self._summary_entry = Gtk.Entry()
        self._summary_entry.connect("changed",
                                    self._on_doc_property_changed)
        _form_row(grid, 1, _("Summary"), self._summary_entry)
        self._suggestions_check = Gtk.CheckButton(
            label=_("Word suggestions bar"))
        self._suggestions_check.connect("toggled",
                                        self._on_doc_property_changed)
        grid.attach(self._suggestions_check, 1, 2, 1, 1)

        props.pack_start(Gtk.Separator(), False, False, 6)
        self._key_heading = _heading(_("Key"))
        props.pack_start(self._key_heading, False, False, 0)

        self._key_grid = grid = _form_grid()
        props.pack_start(grid, False, False, 0)
        row = 0

        self._action_combo = Gtk.ComboBoxText()
        for type, label in ACTION_LABELS:
            self._action_combo.append(type, label)
        self._action_combo.connect("changed", self._on_action_changed)
        _form_row(grid, row, _("Action"), self._action_combo)
        row += 1

        self._value_entry = Gtk.Entry()
        self._value_entry.connect("changed", self._on_value_changed)
        self._template_combo = Gtk.ComboBoxText()
        for id, description in self._get_template_choices():
            self._template_combo.append(id, description)
        self._template_combo.connect("changed", self._on_template_changed)
        self._target_layer_combo = Gtk.ComboBoxText()
        self._target_layer_combo.connect("changed",
                                         self._on_target_layer_changed)
        value_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        value_box.pack_start(self._value_entry, False, False, 0)
        value_box.pack_start(self._template_combo, False, False, 0)
        value_box.pack_start(self._target_layer_combo, False, False, 0)
        self._value_label = _form_row(grid, row, _("Value"), value_box)
        row += 1

        self._hint_label = Gtk.Label(xalign=0.0, wrap=True,
                                     max_width_chars=36)
        self._hint_label.get_style_context().add_class("dim-label")
        grid.attach(self._hint_label, 1, row, 1, 1)
        row += 1

        self._label_entry = Gtk.Entry()
        self._label_entry.set_placeholder_text(_("automatic"))
        self._label_entry.set_tooltip_text(
            _("Text shown on the key; \\n starts a new line"))
        self._label_entry.connect("changed", self._on_label_changed)
        _form_row(grid, row, _("Label"), self._label_entry)
        row += 1

        self._image_entry = Gtk.ComboBoxText.new_with_entry()
        self._image_entry.append_text("")
        for fn in self._get_image_choices():
            self._image_entry.append_text(fn)
        self._image_entry.get_child().set_placeholder_text(_("none"))
        self._image_entry.connect("changed", self._on_image_changed)
        _form_row(grid, row, _("Image"), self._image_entry)
        row += 1

        self._group_entry = Gtk.ComboBoxText.new_with_entry()
        for group in SIZE_GROUPS:
            self._group_entry.append_text(group)
        self._group_entry.set_tooltip_text(
            _("Keys of the same group share a label font size"))
        self._group_entry.connect("changed", self._on_group_changed)
        _form_row(grid, row, _("Size group"), self._group_entry)
        row += 1

        self._id_entry = Gtk.Entry()
        self._id_entry.set_tooltip_text(
            _("Unique within the layer. Ids of special keys pick up "
              "their behavior from key_defs.xml."))
        self._id_entry.connect("changed", self._on_id_changed)
        _form_row(grid, row, _("Id"), self._id_entry)
        row += 1

        geometry = Gtk.Grid(column_spacing=6, row_spacing=4)
        self._geometry_spins = {}
        for i, (name, label) in enumerate([("x", "X"), ("y", "Y"),
                                           ("w", _("Width")),
                                           ("h", _("Height"))]):
            spin = Gtk.SpinButton.new_with_range(0, 200, 0.25)
            spin.set_digits(2)
            spin.set_width_chars(5)
            spin.connect("value-changed", self._on_geometry_spin, name)
            self._geometry_spins[name] = spin
            l = Gtk.Label(label=label, xalign=1.0)
            geometry.attach(l, (i % 2) * 2, i // 2, 1, 1)
            geometry.attach(spin, (i % 2) * 2 + 1, i // 2, 1, 1)
        _form_row(grid, row, _("Position"), geometry)
        row += 1
        units = Gtk.Label(label=_("in key units"), xalign=0.0)
        units.get_style_context().add_class("dim-label")
        grid.attach(units, 1, row, 1, 1)
        row += 1

        expander = Gtk.Expander(label=_("More attributes"))
        self._extra_view = Gtk.TextView(monospace=True,
                                        wrap_mode=Gtk.WrapMode.CHAR)
        self._extra_view.set_tooltip_text(
            _("Further layout attributes, one name=value per line, "
              "e.g. sticky=true or tooltip=Hello"))
        self._extra_view.get_buffer().connect("changed",
                                              self._on_extra_changed)
        extra_frame = Gtk.Frame()
        extra_frame.add(self._extra_view)
        extra_frame.set_size_request(-1, 80)
        expander.add(extra_frame)
        grid.attach(expander, 0, row, 2, 1)

        props.pack_start(Gtk.Separator(), False, False, 6)
        help_label = Gtk.Label(
            xalign=0.0, wrap=True, max_width_chars=40,
            label=_("Double-click empty space to add a key, drag to move, "
                    "drag the corner handle to resize. Ctrl+click or drag "
                    "a frame to select several keys. Arrow keys nudge, "
                    "Ctrl+D duplicates, Del removes.\n\n"
                    "Keyboards saved to the default folder appear in "
                    "Onboard's preferences under Layout → My layouts."))
        help_label.get_style_context().add_class("dim-label")
        props.pack_start(help_label, False, False, 0)

        self.show_all()

    def _get_template_choices(self):
        choices = list(TEMPLATE_KEYS)
        for row in QWERTY_ROWS:
            for item in row:
                id, label = item.split(":", 1)
                choices.append((id, _("Key {} (“{}” on US keyboards)")
                                .format(id, label)))
        return choices

    def _get_image_choices(self):
        if not self.layouts_dir:
            return []
        try:
            return sorted(f for f in os.listdir(
                os.path.join(self.layouts_dir, "images"))
                if f.endswith(".svg"))
        except OSError:
            return []

    # ------------------------------------------------------------------
    # Document state

    def get_current_layer(self):
        return self.doc.layers[self.current_layer]

    def snap_enabled(self):
        return self._snap_check.get_active()

    def _set_document(self, doc):
        self.doc = doc
        self.current_layer = 0
        self.selection = []
        self._undo_stack = []
        self._redo_stack = []
        self._edit_tag = None
        self._dirty = False
        self._update_all()

    def _update_all(self):
        self._updating = True
        try:
            self._name_entry.set_text(self.doc.id)
            self._summary_entry.set_text(self.doc.summary)
            self._suggestions_check.set_active(self.doc.word_suggestions)
            self._update_layer_combos()
        finally:
            self._updating = False
        self._update_key_panel()
        self._update_title()
        self._update_undo_buttons()
        self.canvas.queue_draw()

    def _update_layer_combos(self):
        self._updating = True
        try:
            self._layer_combo.remove_all()
            self._target_layer_combo.remove_all()
            for i, layer in enumerate(self.doc.layers):
                self._layer_combo.append(str(i), layer.name)
                self._target_layer_combo.append(str(i), layer.name)
            self.current_layer = min(self.current_layer,
                                     len(self.doc.layers) - 1)
            self._layer_combo.set_active(self.current_layer)
        finally:
            self._updating = False

    def _update_title(self):
        title = self.doc.id or _("Untitled")
        if self._dirty:
            title = "*" + title
        self._header.set_title(title)
        self._header.set_subtitle(
            self.doc.filename or _("Onboard Layout Editor"))
        self.set_title(title + " – " + _("Onboard Layout Editor"))

    def _update_status(self):
        layer = self.get_current_layer()
        w, h = self.doc.get_extents()
        text = _("{} keys in this layer · keyboard {:g} × {:g} key units") \
            .format(len(layer.keys), w / KEY_UNIT, h / KEY_UNIT)
        if self.selection:
            text += " · " + _("{} selected").format(len(self.selection))
        self._status.set_text(text)

    def set_selection(self, keys):
        if keys != self.selection:
            self.selection = list(keys)
            self._edit_tag = None
            self._update_key_panel()
            self.canvas.queue_draw()

    def toggle_selected(self, key):
        if key in self.selection:
            self.set_selection([k for k in self.selection if k is not key])
        else:
            self.set_selection(self.selection + [key])

    def on_geometry_changed(self):
        self._mark_changed()
        self._update_geometry_spins()
        self.canvas.queue_draw()

    # ------------------------------------------------------------------
    # Undo

    def begin_edit(self, tag=None):
        """
        Remember the state before a change. Consecutive changes with
        the same tag, e.g. typing into one entry, are undone together.
        """
        if tag is not None and tag == self._edit_tag:
            return
        self._undo_stack.append(self._snapshot())
        del self._undo_stack[:-100]
        self._redo_stack = []
        self._edit_tag = tag
        self._edit_pending = True
        self._update_undo_buttons()

    def end_edit(self):
        """ Drop the undo step of a mouse operation that changed nothing. """
        if self._edit_pending and self._undo_stack and \
           not self._edit_tag and \
           self._snapshot_equal(self._undo_stack[-1], self._snapshot()):
            self._undo_stack.pop()
            self._update_undo_buttons()
        self._edit_pending = False

    def _mark_changed(self):
        if not self._dirty:
            self._dirty = True
            self._update_title()
        self._update_status()

    def _snapshot(self):
        layer = self.get_current_layer()
        indices = [layer.keys.index(k) for k in self.selection
                   if k in layer.keys]
        return (self.doc.copy(), self.current_layer, indices)

    @staticmethod
    def _snapshot_equal(a, b):
        da, db = a[0], b[0]
        return da.build_layout_xml() == db.build_layout_xml() and \
            [[(k.x, k.y, k.w, k.h) for k in l.keys] for l in da.layers] == \
            [[(k.x, k.y, k.w, k.h) for k in l.keys] for l in db.layers]

    def _restore(self, snapshot):
        doc, layer_index, indices = snapshot
        filename, imported = self.doc.filename, self.doc.imported
        self.doc = doc.copy()
        self.doc.filename, self.doc.imported = filename, imported
        self.current_layer = min(layer_index, len(self.doc.layers) - 1)
        layer = self.get_current_layer()
        self.selection = [layer.keys[i] for i in indices
                          if i < len(layer.keys)]
        self._edit_tag = None
        self._dirty = True
        self._update_all()

    def _on_undo(self, *args):
        if self._undo_stack:
            self._redo_stack.append(self._snapshot())
            self._restore(self._undo_stack.pop())

    def _on_redo(self, *args):
        if self._redo_stack:
            self._undo_stack.append(self._snapshot())
            self._restore(self._redo_stack.pop())

    def _update_undo_buttons(self):
        self._undo_button.set_sensitive(bool(self._undo_stack))
        self._redo_button.set_sensitive(bool(self._redo_stack))

    # ------------------------------------------------------------------
    # Key operations

    def _free_position(self, w, h):
        """ First grid position, row by row, where a key fits. """
        layer = self.get_current_layer()
        for row in range(50):
            for col in range(0, 200):
                x = col * SNAP + KEY_GAP / 2
                y = row * KEY_UNIT + KEY_GAP / 2
                if not any(k.x < x + w and k.x + k.w > x and
                           k.y < y + h and k.y + k.h > y
                           for k in layer.keys):
                    return x, y
        return KEY_GAP / 2, KEY_GAP / 2

    def add_key_at(self, x, y, id=None):
        """ Add a key at model position x, y or at the next free spot. """
        layer = self.get_current_layer()
        if id and layer.find_key(id):
            self._show_status_message(
                _("Layer “{}” already has a key “{}”.")
                .format(layer.name, id))
            return None
        self.begin_edit()
        size = KEY_UNIT - KEY_GAP
        if x is None:
            x, y = self._free_position(size, size)
        else:
            x = max(0.0, snap(x - KEY_UNIT / 2) if self.snap_enabled()
                    else x - size / 2)
            y = max(0.0, snap(y - KEY_UNIT / 2) if self.snap_enabled()
                    else y - size / 2)
            x += KEY_GAP / 2
            y += KEY_GAP / 2
        key = EditorKey(id or layer.new_key_id(), x, y)
        if id:
            if LAYER_KEY_PATTERN.match(id):
                key.attributes["button"] = "true"
        else:
            key.attributes["char"] = ""
        layer.keys.append(key)
        self._mark_changed()
        self.set_selection([key])
        self.canvas.queue_draw()
        if not id:
            self._value_entry.grab_focus()
        return key

    def _on_add_template_key(self, item, id):
        key = self.add_key_at(None, None, id)
        if key and id in ("SPCE",):
            key.w = 5 * KEY_UNIT - KEY_GAP
            self._update_key_panel()
            self.canvas.queue_draw()

    def delete_selection(self):
        if not self.selection:
            return
        self.begin_edit()
        layer = self.get_current_layer()
        layer.keys = [k for k in layer.keys if k not in self.selection]
        self._mark_changed()
        self.set_selection([])

    def duplicate_selection(self):
        if not self.selection:
            return
        self.begin_edit()
        layer = self.get_current_layer()
        top = max(k.y + k.h for k in layer.keys)
        min_y = min(k.y for k in self.selection)
        dy = snap(top - min_y + KEY_GAP, KEY_UNIT)
        new_keys = []
        for key in self.selection:
            id = key.id
            if layer.find_key(id) or any(k.id == id for k in new_keys):
                id = layer.new_key_id()
                while any(k.id == id for k in new_keys):
                    id = "k" + str(int(id[1:]) + 1)
            k = EditorKey(id, key.x, key.y + dy, key.w, key.h,
                          key.attributes)
            new_keys.append(k)
        layer.keys.extend(new_keys)
        self._mark_changed()
        self.set_selection(new_keys)

    def nudge_selection(self, dx, dy):
        if not self.selection:
            return
        self.begin_edit("nudge")
        min_x = min(k.x for k in self.selection)
        min_y = min(k.y for k in self.selection)
        dx = max(dx, KEY_GAP / 2 - min_x)
        dy = max(dy, KEY_GAP / 2 - min_y)
        for k in self.selection:
            k.x += dx
            k.y += dy
        self.on_geometry_changed()

    def focus_label_entry(self):
        self._label_entry.grab_focus()

    def _show_status_message(self, text):
        self._status.set_text(text)

    # ------------------------------------------------------------------
    # Key properties panel

    def _update_key_panel(self):
        self._updating = True
        try:
            self._do_update_key_panel()
        finally:
            self._updating = False
        self._update_status()

    def _do_update_key_panel(self):
        keys = self.selection
        single = len(keys) == 1
        key = keys[0] if keys else None

        self._key_grid.set_sensitive(bool(keys))
        if not keys:
            self._key_heading.set_text(_("Key"))
        elif single:
            self._key_heading.set_text(_("Key “{}”").format(key.id))
        else:
            self._key_heading.set_text(_("{} keys").format(len(keys)))

        for widget in (self._action_combo, self._value_entry,
                       self._template_combo, self._target_layer_combo,
                       self._label_entry, self._image_entry,
                       self._id_entry, self._extra_view,
                       self._geometry_spins["x"],
                       self._geometry_spins["y"]):
            widget.set_sensitive(single)

        type = key.get_action_type() if single else "char"
        self._action_combo.set_active_id(type if single else None)
        self._value_entry.set_visible(type not in ("template", "layer",
                                                   "none"))
        self._template_combo.set_visible(type == "template")
        self._target_layer_combo.set_visible(type == "layer")
        self._value_label.set_visible(type != "none")
        self._hint_label.set_text(ACTION_HINTS.get(type, "")
                                  if single else "")

        if single:
            self._value_entry.set_text(key.get_action_value())
            if type == "template":
                if not self._template_combo.set_active_id(key.id):
                    self._template_combo.set_active(-1)
            if type == "layer":
                self._target_layer_combo.set_active_id(
                    key.get_action_value())
            self._label_entry.set_text(key.attributes.get("label", ""))
            self._image_entry.get_child().set_text(
                key.attributes.get("image", ""))
            self._id_entry.set_text(key.id)
            self._extra_view.get_buffer().set_text(
                "\n".join("{}={}".format(k, v)
                          for k, v in sorted(key.attributes.items())
                          if k not in MANAGED_ATTRIBUTES))
        else:
            self._value_entry.set_text("")
            self._label_entry.set_text("")
            self._image_entry.get_child().set_text("")
            self._id_entry.set_text("")
            self._extra_view.get_buffer().set_text("")

        groups = {k.attributes.get("group", "") for k in keys}
        self._group_entry.get_child().set_text(
            groups.pop() if len(groups) == 1 else "")

        self._update_geometry_spins()

    def _update_geometry_spins(self):
        keys = self.selection
        key = keys[0] if keys else None
        was_updating = self._updating
        self._updating = True
        try:
            values = {"x": 0, "y": 0, "w": 1, "h": 1}
            if key:
                values = {"x": (key.x - KEY_GAP / 2) / KEY_UNIT,
                          "y": (key.y - KEY_GAP / 2) / KEY_UNIT,
                          "w": (key.w + KEY_GAP) / KEY_UNIT,
                          "h": (key.h + KEY_GAP) / KEY_UNIT}
            for name, spin in self._geometry_spins.items():
                spin.set_value(values[name])
        finally:
            self._updating = was_updating

    def _single_key(self, tag):
        """ The selected key, after preparing undo, or None. """
        if self._updating or len(self.selection) != 1:
            return None
        self.begin_edit(tag)
        return self.selection[0]

    def _key_changed(self):
        self._mark_changed()
        self.canvas.queue_draw()
        if self.selection:
            self._key_heading.set_text(
                _("Key “{}”").format(self.selection[0].id)
                if len(self.selection) == 1
                else _("{} keys").format(len(self.selection)))

    def _on_action_changed(self, combo):
        key = self._single_key(None)
        if not key:
            return
        type = combo.get_active_id()
        old_type = key.get_action_type()
        if type == old_type:
            return
        layer = self.get_current_layer()

        # Leave template ids behind, they would inherit template
        # attributes like modifiers or labels.
        if old_type in ("template", "layer") and \
           type not in ("template", "layer"):
            key.id = layer.new_key_id()

        if type == "template":
            free = [id for id, d in TEMPLATE_KEYS
                    if not layer.find_key(id)]
            key.set_action("template", None)
            key.id = free[0] if free else layer.new_key_id()
        elif type == "layer":
            free = [i for i in range(len(self.doc.layers))
                    if i != self.current_layer and
                    not layer.find_key("layer{}".format(i))]
            index = free[0] if free else 0
            key.id = "layer{}".format(index)
            key.set_action("layer", None)
        elif type == "none":
            key.set_action("none", None)
        else:
            key.set_action(type, "")

        self._key_changed()
        self._update_key_panel()

    def _on_value_changed(self, entry):
        key = self._single_key("value")
        if not key:
            return
        type = key.get_action_type()
        if type in ("char", "keycode", "keysym", "keypress_name", "macro"):
            key.set_action(type, entry.get_text())
            self._key_changed()

    def _on_template_changed(self, combo):
        key = self._single_key(None)
        id = combo.get_active_id()
        if not key or not id or id == key.id:
            return
        other = self.get_current_layer().find_key(id)
        if other and other is not key:
            self._show_status_message(
                _("Layer “{}” already has a key “{}”.")
                .format(self.get_current_layer().name, id))
        key.id = id
        self._key_changed()
        self._update_key_panel()

    def _on_target_layer_changed(self, combo):
        key = self._single_key(None)
        index = combo.get_active_id()
        if not key or index is None:
            return
        key.id = "layer" + index
        key.attributes["button"] = "true"
        self._key_changed()
        self._update_key_panel()

    def _on_label_changed(self, entry):
        key = self._single_key("label")
        if not key:
            return
        _set_or_remove(key.attributes, "label", entry.get_text())
        self._key_changed()

    def _on_image_changed(self, combo):
        key = self._single_key("image")
        if not key:
            return
        _set_or_remove(key.attributes, "image",
                       combo.get_child().get_text().strip())
        self._key_changed()

    def _on_group_changed(self, combo):
        if self._updating or not self.selection:
            return
        self.begin_edit("group")
        group = combo.get_child().get_text().strip()
        for key in self.selection:
            _set_or_remove(key.attributes, "group", group)
        self._key_changed()

    def _on_id_changed(self, entry):
        key = self._single_key("id")
        if not key:
            return
        key.id = entry.get_text().strip()
        self._key_changed()

    def _on_extra_changed(self, buffer):
        key = self._single_key("extra")
        if not key:
            return
        text = buffer.get_text(buffer.get_start_iter(),
                               buffer.get_end_iter(), False)
        for k in [k for k in key.attributes if k not in MANAGED_ATTRIBUTES]:
            del key.attributes[k]
        for line in text.splitlines():
            name, sep, value = line.partition("=")
            name = name.strip()
            if sep and name and name not in MANAGED_ATTRIBUTES and \
               all(c.isalnum() or c in "_-" for c in name):
                key.attributes[name] = value.strip()
        self._key_changed()

    def _on_geometry_spin(self, spin, name):
        if self._updating or not self.selection:
            return
        self.begin_edit("geometry-" + name)
        value = spin.get_value()
        for key in self.selection:
            if name == "x":
                key.x = value * KEY_UNIT + KEY_GAP / 2
            elif name == "y":
                key.y = value * KEY_UNIT + KEY_GAP / 2
            elif name == "w":
                key.w = max(1.0, value * KEY_UNIT - KEY_GAP)
            elif name == "h":
                key.h = max(1.0, value * KEY_UNIT - KEY_GAP)
        self._mark_changed()
        self.canvas.queue_draw()

    def _on_doc_property_changed(self, widget):
        if self._updating:
            return
        self.begin_edit("doc-" + str(id(widget)))
        self.doc.id = self._name_entry.get_text().strip()
        self.doc.summary = self._summary_entry.get_text()
        self.doc.word_suggestions = self._suggestions_check.get_active()
        self._mark_changed()
        self._update_title()

    # ------------------------------------------------------------------
    # Layers

    def _on_layer_changed(self, combo):
        if self._updating or combo.get_active() < 0:
            return
        self.current_layer = combo.get_active()
        self.selection = []
        self._edit_tag = None
        self._update_key_panel()
        self.canvas.queue_draw()

    def _on_add_layer(self, button):
        name = self._ask_text(_("Add Layer"), _("Name of the new layer:"),
                              self._unused_layer_name())
        if not name:
            return
        self.begin_edit()
        self.doc.layers.append(EditorLayer(name))
        self.current_layer = len(self.doc.layers) - 1
        self._after_layers_changed()

    def _on_duplicate_layer(self, button):
        name = self._ask_text(_("Duplicate Layer"),
                              _("Name of the new layer:"),
                              self._unused_layer_name())
        if not name:
            return
        self.begin_edit()
        layer = self.get_current_layer()
        self.doc.layers.append(EditorLayer(
            name, [EditorKey(k.id, k.x, k.y, k.w, k.h, k.attributes)
                   for k in layer.keys]))
        self.current_layer = len(self.doc.layers) - 1
        self._after_layers_changed()

    def _on_rename_layer(self, button):
        layer = self.get_current_layer()
        name = self._ask_text(_("Rename Layer"), _("Name of the layer:"),
                              layer.name)
        if not name or name == layer.name:
            return
        self.begin_edit()
        layer.name = name
        self._after_layers_changed()

    def _on_remove_layer(self, button):
        if len(self.doc.layers) <= 1:
            self._show_status_message(_("A keyboard needs at least "
                                        "one layer."))
            return
        layer = self.get_current_layer()
        if layer.keys and not self._confirm(
                _("Remove layer “{}” and its {} keys?")
                .format(layer.name, len(layer.keys)), _("Remove")):
            return
        self.begin_edit()
        removed = self.current_layer
        del self.doc.layers[removed]

        # Keep layer buttons pointing at the same layers.
        for l in self.doc.layers:
            for key in l.keys:
                m = LAYER_KEY_PATTERN.match(key.id)
                if m and int(m.group(1)) > removed:
                    key.id = "layer{}".format(int(m.group(1)) - 1)
        self.current_layer = max(0, removed - 1)
        self._after_layers_changed()

    def _after_layers_changed(self):
        self.selection = []
        self._mark_changed()
        self._update_layer_combos()
        self._update_key_panel()
        self.canvas.queue_draw()

    def _unused_layer_name(self):
        names = {n.lower() for n in self.doc.get_layer_names()}
        for name in ("Numbers", "Symbols", "Functions", "Emoji"):
            if name.lower() not in names:
                return name
        i = len(self.doc.layers) + 1
        while "layer {}".format(i) in names:
            i += 1
        return "Layer {}".format(i)

    # ------------------------------------------------------------------
    # Keyboard shortcuts

    def _on_key_press(self, widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        keyval = event.keyval
        name = Gdk.keyval_name(keyval) or ""

        if ctrl:
            if name.lower() == "z" and not shift:
                self._on_undo()
                return True
            if name.lower() == "y" or (name.lower() == "z" and shift):
                self._on_redo()
                return True
            if name.lower() == "s":
                self._on_save_as(None) if shift else self._on_save(None)
                return True
            if name.lower() == "o":
                self._on_open(None)
                return True

        if not self.canvas.has_focus():
            return False

        if ctrl and name.lower() == "a":
            self.set_selection(list(self.get_current_layer().keys))
            return True
        if ctrl and name.lower() == "d":
            self.duplicate_selection()
            return True
        if name in ("Delete", "BackSpace"):
            self.delete_selection()
            return True
        if name == "Escape":
            self.set_selection([])
            return True
        step = KEY_UNIT if shift else (SNAP if self.snap_enabled() else 1.0)
        moves = {"Left": (-step, 0), "Right": (step, 0),
                 "Up": (0, -step), "Down": (0, step)}
        if name in moves:
            self.nudge_selection(*moves[name])
            return True
        return False

    # ------------------------------------------------------------------
    # Files

    def _on_new_qwerty(self, item):
        if self._confirm_discard():
            self._set_document(EditorDocument.new_qwerty())

    def _on_new_empty(self, item):
        if self._confirm_discard():
            self._set_document(EditorDocument())

    def _on_new_grid(self, item):
        if not self._confirm_discard():
            return
        dialog = Gtk.Dialog(title=_("Grid of Keys"), transient_for=self,
                            modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                           _("Create"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = _form_grid()
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        rows = Gtk.SpinButton.new_with_range(1, 20, 1)
        rows.set_value(4)
        columns = Gtk.SpinButton.new_with_range(1, 30, 1)
        columns.set_value(10)
        _form_row(grid, 0, _("Rows"), rows)
        _form_row(grid, 1, _("Columns"), columns)
        dialog.get_content_area().add(grid)
        dialog.show_all()
        response = dialog.run()
        r, c = rows.get_value_as_int(), columns.get_value_as_int()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self._set_document(EditorDocument.new_grid(r, c))

    def _on_open(self, button):
        if not self._confirm_discard():
            return
        dialog = Gtk.FileChooserDialog(
            title=_("Open Layout"), transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                           _("Open"), Gtk.ResponseType.ACCEPT)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_("Onboard layouts"))
        file_filter.add_pattern("*.onboard")
        dialog.add_filter(file_filter)
        folder = get_user_layouts_dir()
        if not os.path.isdir(folder):
            folder = self.layouts_dir
        if folder:
            dialog.set_current_folder(folder)
        if self.layouts_dir:
            try:
                dialog.add_shortcut_folder(self.layouts_dir)
            except GLib.Error:
                pass
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and filename:
            self._load(filename)

    def _load(self, filename):
        try:
            doc = EditorDocument.load(filename)
        except Exception as ex:
            _logger.warning("Failed to load '{}': {}".format(filename, ex))
            self._error(_("Can't open “{}”.").format(filename), str(ex))
            return False
        self._set_document(doc)
        if doc.imported:
            self._show_status_message(
                _("Imported. Popups and keymap-specific key variants "
                  "aren't kept; saving creates a new layout."))
        return True

    def _on_save(self, button):
        return self._save(ask=False)

    def _on_save_as(self, button):
        return self._save(ask=True)

    def _save(self, ask):
        """ Save, asking for a file name when needed. True on success. """
        problems = self.doc.validate()
        if problems:
            self._error(_("The keyboard can't be saved yet."),
                        "\n".join("• " + p for p in problems[:10]))
            return False

        filename = self.doc.filename
        if ask or not filename or self.doc.imported or \
           not os.access(os.path.dirname(filename), os.W_OK):
            filename = self._ask_save_filename()
            if not filename:
                return False

        try:
            self.doc.save(filename)
        except OSError as ex:
            self._error(_("Saving failed."), str(ex))
            return False
        _logger.info("Saved layout '{}'".format(filename))
        self._dirty = False
        self._update_title()
        self._show_status_message(_("Saved {}").format(filename))
        return True

    def _ask_save_filename(self):
        folder = get_user_layouts_dir()
        dialog = Gtk.FileChooserDialog(
            title=_("Save Layout"), transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                           _("Save"), Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        os.makedirs(folder, exist_ok=True)
        dialog.set_current_folder(folder)
        name = self.doc.id
        if self.doc.imported:
            name = self.doc.id.replace(",", "").replace(" ", "_") + "-custom"
        dialog.set_current_name(name + ".onboard")
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not filename:
            return None
        if not filename.endswith(".onboard"):
            filename += ".onboard"

        # The file name and keyboard id must agree, SVG names derive
        # from the id.
        base = os.path.splitext(os.path.basename(filename))[0]
        if base != self.doc.id:
            self.doc.id = base
            self._updating = True
            self._name_entry.set_text(base)
            self._updating = False
        return filename

    def _on_try(self, button):
        if (self._dirty or not self.doc.filename or self.doc.imported) and \
           not self._save(ask=False):
            return
        self._stop_preview()
        command = self._get_onboard_command()
        if not command:
            self._error(_("Onboard wasn't found."),
                        _("Install Onboard or run the editor from "
                          "the Onboard source tree."))
            return
        command += ["--allow-multiple-instances",
                    "--layout", self.doc.filename]
        _logger.info("Starting preview: {}".format(command))
        try:
            self._preview_process = subprocess.Popen(command)
        except OSError as ex:
            self._error(_("Onboard couldn't be started."), str(ex))
            return
        self._show_status_message(
            _("Opened in Onboard. Click “Try in Onboard” again "
              "after changes to reload."))

    def _get_onboard_command(self):
        script = os.path.join(PROJECT_DIR, "onboard")
        if os.path.isfile(script):
            return [sys.executable, script]
        path = shutil.which("onboard")
        return [path] if path else None

    def _stop_preview(self):
        process = self._preview_process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._preview_process = None

    def _on_delete_event(self, widget, event):
        if not self._confirm_discard():
            return True
        self._stop_preview()
        Gtk.main_quit()
        return False

    # ------------------------------------------------------------------
    # Dialogs

    def _confirm_discard(self):
        """ True if there are no unsaved changes or the user lets go. """
        if not self._dirty:
            return True
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            text=_("Save changes to “{}”?").format(self.doc.id))
        dialog.add_buttons(_("Discard"), Gtk.ResponseType.REJECT,
                           _("Cancel"), Gtk.ResponseType.CANCEL,
                           _("Save"), Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            return self._save(ask=False)
        return response == Gtk.ResponseType.REJECT

    def _confirm(self, text, action_label):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   text=text)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                           action_label, Gtk.ResponseType.ACCEPT)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.ACCEPT

    def _error(self, text, details=""):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE,
                                   text=text, secondary_text=details)
        dialog.run()
        dialog.destroy()

    def _ask_text(self, title, prompt, default=""):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                           _("OK"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin=12)
        box.pack_start(Gtk.Label(label=prompt, xalign=0.0), False, False, 0)
        entry = Gtk.Entry(text=default, activates_default=True)
        box.pack_start(entry, False, False, 0)
        dialog.get_content_area().add(box)
        dialog.show_all()
        response = dialog.run()
        text = entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and text:
            if text.lower() in (n.lower() for n in
                                self.doc.get_layer_names()) and \
               text != default:
                self._error(_("A layer named “{}” exists already.")
                            .format(text))
                return None
            return text
        return None


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _set_or_remove(attributes, name, value):
    if value:
        attributes[name] = value
    else:
        attributes.pop(name, None)


def _icon_button(icon_name, tooltip, callback):
    button = Gtk.Button.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
    button.set_tooltip_text(tooltip)
    button.connect("clicked", callback)
    return button


def _heading(text):
    label = Gtk.Label(label=text, xalign=0.0)
    attributes = Pango.AttrList()
    attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    label.set_attributes(attributes)
    return label


def _form_grid():
    return Gtk.Grid(column_spacing=12, row_spacing=6)


def _form_row(grid, row, label, widget):
    label_widget = Gtk.Label(label=label, xalign=1.0)
    widget.set_hexpand(True)
    grid.attach(label_widget, 0, row, 1, 1)
    grid.attach(widget, 1, row, 1, 1)
    return label_widget


def main(argv):
    from Onboard.utils import Translation
    Translation.install("onboard")
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    filename = argv[1] if len(argv) > 1 else None
    LayoutEditor(filename)
    Gtk.main()
