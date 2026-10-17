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
Toolkit independent document model of the layout editor.

Reads and writes Onboard layouts (*.onboard) together with one SVG
geometry file per layer. Layouts written here have a fixed, simple
structure; other layouts are imported best-effort by collecting the
keys of their layer panels.
"""

import os
import re
import copy
from xml.dom import minidom
from xml.sax.saxutils import quoteattr

import logging
_logger = logging.getLogger(__name__)


LAYOUT_FORMAT = "3.2"

# Key pitch in SVG units; one key occupies KEY_UNIT minus KEY_GAP.
KEY_UNIT = 20.0
KEY_GAP = 2.0
SNAP = KEY_UNIT / 4.0

WORDLIST_HEIGHT = 15.0

# Ways a key can act, mapped to the layout attribute holding its value.
# "template" keys get everything from a key_template in key_defs.xml,
# matched by key id.
ACTION_TYPES = [
    ("template", None),
    ("char", "char"),
    ("keycode", "keycode"),
    ("keysym", "keysym"),
    ("keypress_name", "keypress_name"),
    ("macro", "macro"),
    ("layer", None),
    ("none", None),
]
ACTION_ATTRIBUTES = [a for t, a in ACTION_TYPES if a] + \
                    ["button", "target_layer", "script"]

# Attributes the editor manages itself; everything else is passed
# through untouched as "extra" attributes.
MANAGED_ATTRIBUTES = set(ACTION_ATTRIBUTES) | {"id", "label", "group",
                                               "image"}

LAYER_KEY_PATTERN = re.compile(r"^layer(\d+)$")

# Templates offered in the editor's palette, (id, description).
TEMPLATE_KEYS = [
    ("LFSH", "Shift (left)"),
    ("RTSH", "Shift (right)"),
    ("CAPS", "Caps Lock"),
    ("LCTL", "Ctrl (left)"),
    ("RCTL", "Ctrl (right)"),
    ("LALT", "Alt"),
    ("RALT", "AltGr"),
    ("LWIN", "Super"),
    ("MENU", "Menu"),
    ("TAB", "Tab"),
    ("BKSP", "Backspace"),
    ("RTRN", "Return"),
    ("SPCE", "Space"),
    ("ESC", "Escape"),
    ("DELE", "Delete"),
    ("INS", "Insert"),
    ("HOME", "Home"),
    ("END", "End"),
    ("PGUP", "Page Up"),
    ("PGDN", "Page Down"),
    ("LEFT", "Arrow left"),
    ("RGHT", "Arrow right"),
    ("UP", "Arrow up"),
    ("DOWN", "Arrow down"),
    ("NMLK", "Num Lock"),
    ("Prnt", "Print"),
    ("F1", "F1"), ("F2", "F2"), ("F3", "F3"), ("F4", "F4"),
    ("F5", "F5"), ("F6", "F6"), ("F7", "F7"), ("F8", "F8"),
    ("F9", "F9"), ("F10", "F10"), ("F11", "F11"), ("F12", "F12"),
    ("settings", "Open Onboard preferences"),
    ("hide", "Hide Onboard"),
    ("move", "Move Onboard"),
    ("quit", "Quit Onboard"),
    ("showclick", "Toggle click helpers"),
    ("middleclick", "Middle click"),
    ("secondaryclick", "Right click"),
    ("doubleclick", "Double click"),
    ("dragclick", "Drag click"),
    ("hoverclick", "Hover click"),
]

# Short labels to draw in the editor for template keys without
# an obvious label.
TEMPLATE_LABELS = {
    "LFSH": "⇧", "RTSH": "⇧", "CAPS": "⇪", "LCTL": "Ctrl", "RCTL": "Ctrl",
    "LALT": "Alt", "RALT": "AltGr", "LWIN": "Win", "MENU": "Menu",
    "TAB": "⇥", "BKSP": "⌫", "RTRN": "⏎", "SPCE": "Space", "ESC": "Esc",
    "DELE": "Del", "INS": "Ins", "HOME": "Hm", "END": "End",
    "PGUP": "PgUp", "PGDN": "PgDn", "LEFT": "←", "RGHT": "→", "UP": "↑",
    "DOWN": "↓", "NMLK": "NmLk", "Prnt": "Prnt",
    "settings": "⚙", "hide": "✕", "move": "✥", "quit": "Quit",
    "showclick": "🖱", "middleclick": "M", "secondaryclick": "R",
    "doubleclick": "2×", "dragclick": "Drag", "hoverclick": "Hover",
}

# Rows of the ISO/ANSI alpha block as XKB key names, each tagged with
# the label shown for a US keymap. Onboard relabels keycode keys from
# the active system keymap, so these presets adapt to any language.
QWERTY_ROWS = [
    ["TLDE:`"] + ["AE{:02}:{}".format(i + 1, c)
                  for i, c in enumerate("1234567890-=")],
    ["AD{:02}:{}".format(i + 1, c) for i, c in enumerate("qwertyuiop[]")],
    ["AC{:02}:{}".format(i + 1, c) for i, c in enumerate("asdfghjkl;'")]
    + ["BKSL:\\"],
    ["AB{:02}:{}".format(i + 1, c) for i, c in enumerate("zxcvbnm,./")],
]

US_LABELS = dict(k.split(":", 1) for row in QWERTY_ROWS for k in row)


class EditorKey:
    """ One key, its geometry in SVG units and its layout attributes. """

    def __init__(self, id, x=0.0, y=0.0, w=KEY_UNIT - KEY_GAP,
                 h=KEY_UNIT - KEY_GAP, attributes=None):
        self.id = id
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.attributes = dict(attributes or {})

    def get_action_type(self):
        """ The editor's action type, see ACTION_TYPES. """
        a = self.attributes
        if LAYER_KEY_PATTERN.match(self.id):
            return "layer"
        for type, attribute in ACTION_TYPES:
            if attribute and attribute in a:
                return type
        if "button" in a or "target_layer" in a or "script" in a:
            return "template"
        if self.id in TEMPLATE_LABELS or self.id in US_LABELS:
            return "template"
        return "none"

    def get_action_value(self):
        """ Value of the attribute that defines the key's action. """
        attribute = dict(ACTION_TYPES).get(self.get_action_type())
        if attribute:
            return self.attributes.get(attribute, "")
        if self.get_action_type() == "layer":
            return LAYER_KEY_PATTERN.match(self.id).group(1)
        return ""

    def set_action(self, type, value):
        """
        Replace the key's action. Value is the attribute value for
        char/keycode/... types and the layer index for "layer".
        """
        for attribute in ACTION_ATTRIBUTES:
            self.attributes.pop(attribute, None)
        attribute = dict(ACTION_TYPES).get(type)
        if attribute:
            self.attributes[attribute] = value
        elif type == "layer":
            self.attributes["button"] = "true"

    def get_display_label(self, layer_names=()):
        """ Best guess at what Onboard will show on the key. """
        label = self.attributes.get("label")
        if label:
            return label
        if self.get_action_type() == "layer":
            index = int(LAYER_KEY_PATTERN.match(self.id).group(1))
            if index < len(layer_names):
                return layer_names[index]
            return self.id
        if "char" in self.attributes:
            return self.attributes["char"]
        if self.id in US_LABELS:
            return US_LABELS[self.id]
        if self.id in TEMPLATE_LABELS:
            return TEMPLATE_LABELS[self.id]
        if "image" in self.attributes:
            return "🖼"
        return self.id

    def contains(self, x, y):
        return self.x <= x < self.x + self.w and \
            self.y <= y < self.y + self.h


class EditorLayer:
    """ One layer of the keyboard, written to its own SVG file. """

    def __init__(self, name, keys=None):
        self.name = name
        self.keys = list(keys or [])

    def find_key(self, id):
        for key in self.keys:
            if key.id == id:
                return key
        return None

    def key_at(self, x, y):
        for key in reversed(self.keys):   # topmost first
            if key.contains(x, y):
                return key
        return None

    def new_key_id(self, prefix="k"):
        """ Unused key id, Onboard ids must not contain underscores. """
        ids = {k.id for k in self.keys}
        i = 1
        while "{}{}".format(prefix, i) in ids:
            i += 1
        return "{}{}".format(prefix, i)


class EditorDocument:
    """ The keyboard being edited. """

    def __init__(self, id="MyKeyboard"):
        self.id = id
        self.summary = ""
        self.word_suggestions = False
        self.layers = [EditorLayer("Alpha")]
        self.filename = None    # path of the .onboard file, if saved
        self.imported = False   # loaded from a non-editor layout

    def copy(self):
        return copy.deepcopy(self)

    def get_layer_names(self):
        return [layer.name for layer in self.layers]

    def get_extents(self):
        """ (width, height) of the key area, shared by all layers. """
        w = h = 0.0
        for layer in self.layers:
            for key in layer.keys:
                w = max(w, key.x + key.w)
                h = max(h, key.y + key.h)
        return w + KEY_GAP / 2, h + KEY_GAP / 2

    def validate(self):
        """ List of problems that would keep Onboard from loading it. """
        problems = []
        if not self.id or not re.match(r"^[\w\- ]+$", self.id):
            problems.append("The keyboard name may only contain letters, "
                            "digits, spaces, '-' and '_'.")
        names = [l.name for l in self.layers]
        if len(set(n.lower() for n in names)) != len(names):
            problems.append("Layer names must be unique.")
        for layer in self.layers:
            if not layer.name or "/" in layer.name:
                problems.append("Invalid layer name '{}'."
                                .format(layer.name))
            seen = set()
            for key in layer.keys:
                if not key.id:
                    problems.append("A key in layer '{}' has no id."
                                    .format(layer.name))
                elif key.id in seen:
                    problems.append("Duplicate key id '{}' in layer '{}'."
                                    .format(key.id, layer.name))
                elif re.search(r"[\s\"'<>&]", key.id):
                    problems.append("Key id '{}' in layer '{}' must not "
                                    "contain spaces or quotes."
                                    .format(key.id, layer.name))
                seen.add(key.id)
                m = LAYER_KEY_PATTERN.match(key.id)
                if m and int(m.group(1)) >= len(self.layers):
                    problems.append("Key '{}' in layer '{}' switches to a "
                                    "layer that doesn't exist."
                                    .format(key.id, layer.name))
        if not any(layer.keys for layer in self.layers):
            problems.append("The keyboard has no keys.")
        return problems

    # ------------------------------------------------------------------
    # Saving

    def get_svg_basename(self, layer):
        return "{}-{}.svg".format(self.id, _safe_name(layer.name))

    def get_wordlist_svg_basename(self):
        return "{}-Suggestions.svg".format(self.id)

    def save(self, filename):
        """ Write the .onboard file and its SVGs next to it. """
        directory = os.path.dirname(os.path.abspath(filename))
        os.makedirs(directory, exist_ok=True)

        width, height = self.get_extents()
        written = []

        for layer in self.layers:
            path = os.path.join(directory, self.get_svg_basename(layer))
            _write_text(path, self._build_layer_svg(layer, width, height))
            written.append(path)

        if self.word_suggestions:
            path = os.path.join(directory, self.get_wordlist_svg_basename())
            _write_text(path, self._build_wordlist_svg(width))
            written.append(path)

        _write_text(filename, self.build_layout_xml())
        written.append(filename)

        self.filename = filename
        self.imported = False
        return written

    def build_layout_xml(self):
        lines = []
        add = lines.append
        add("<?xml version='1.0' encoding='UTF-8'?>")
        add("<!-- Created with the Onboard layout editor -->")
        add("<keyboard id={} format='{}' section='user' summary={}>"
            .format(quoteattr(self.id), LAYOUT_FORMAT,
                    quoteattr(self.summary)))
        add("")
        add("    <include file='key_defs.xml'/>")
        add("")
        add("    <box border='0.5' spacing='1.5' orientation='vertical'>")
        if self.word_suggestions:
            add("        <panel filename={}>"
                .format(quoteattr(self.get_wordlist_svg_basename())))
            add("            <include file='word_suggestions.xml'/>")
            add("        </panel>")
        add("        <panel>")
        for layer in self.layers:
            add("            <panel layer={} filename={}>"
                .format(quoteattr(_safe_name(layer.name).lower()),
                        quoteattr(self.get_svg_basename(layer))))
            for key in layer.keys:
                add("                " + self._build_key_xml(key))
            add("            </panel>")
        add("        </panel>")
        add("    </box>")
        add("</keyboard>")
        add("")
        return "\n".join(lines)

    def _build_key_xml(self, key):
        attributes = [("id", key.id)]
        attributes += sorted((k, v) for k, v in key.attributes.items()
                             if k != "id")
        return "<key {}/>".format(" ".join("{}={}".format(k, quoteattr(v))
                                           for k, v in attributes))

    def _build_layer_svg(self, layer, width, height):
        rects = [_svg_rect(key.id, key.x, key.y, key.w, key.h,
                           _key_fill(key))
                 for key in layer.keys]
        return _svg_document(width, height, rects)

    def _build_wordlist_svg(self, width):
        """ Geometry for the keys of word_suggestions.xml. """
        h = WORDLIST_HEIGHT
        y = KEY_GAP / 2
        x0 = KEY_GAP / 2
        x1 = width - KEY_GAP / 2
        button_w = 12.0
        rects = [_svg_rect("wordlist", x0, y, x1 - x0, h, "#d0d0d0")]
        x = x1
        for id in ["hide.wordlist", "language.wordlist",
                   "pause-learning.wordlist", "next-predictions.wordlist",
                   "previous-predictions.wordlist"]:
            x -= button_w
            rects.append(_svg_rect(id, x, y, button_w, h, "#b0b0b0"))
            x -= KEY_GAP
        rects.append(_svg_rect("correction", x0, y, 27, h, "#e0e0e0"))
        rects.append(_svg_rect("expand-corrections", x0 + 29, y, 11, h,
                               "#e0e0e0"))
        rects.append(_svg_rect("prediction", x0 + 42, y, 27, h, "#e0e0e0"))
        return _svg_document(width, h + KEY_GAP, rects)

    # ------------------------------------------------------------------
    # Loading

    @classmethod
    def load(cls, filename):
        """ Load an Onboard layout, see the module docstring. """
        directory = os.path.dirname(os.path.abspath(filename))
        dom = minidom.parse(filename).documentElement
        if dom.tagName != "keyboard":
            raise ValueError("'{}' is not an Onboard layout."
                             .format(filename))

        doc = cls(dom.getAttribute("id") or
                  os.path.splitext(os.path.basename(filename))[0])
        doc.summary = dom.getAttribute("summary")
        doc.layers = []
        doc.imported = True

        with open(filename, encoding="UTF-8") as f:
            if "Created with the Onboard layout editor" in f.read(300):
                doc.imported = False

        svg_cache = {}

        def get_svg_rects(svg_filename):
            if svg_filename not in svg_cache:
                path = os.path.join(directory, svg_filename)
                try:
                    svg_cache[svg_filename] = _read_svg_rects(path)
                except (OSError, ValueError) as ex:
                    _logger.warning("Failed to read '{}': {}"
                                    .format(path, ex))
                    svg_cache[svg_filename] = {}
            return svg_cache[svg_filename]

        def collect_keys(node, svg_filename, layer):
            for child in node.childNodes:
                if child.nodeType != minidom.Node.ELEMENT_NODE:
                    continue
                if child.tagName == "key":
                    attributes = dict(child.attributes.items())
                    id = attributes.pop("id", "")
                    svg_id = attributes.get("svg_id", id)
                    rect = get_svg_rects(svg_filename).get(svg_id) \
                        if svg_filename else None
                    if rect and not layer.find_key(id):
                        layer.keys.append(EditorKey(id, *rect,
                                                    attributes=attributes))
                elif child.tagName in ("panel", "box"):
                    if child.tagName == "panel" and \
                       child.getAttribute("layer"):
                        continue    # handled as a separate layer
                    collect_keys(child,
                                 child.getAttribute("filename") or
                                 svg_filename, layer)

        def is_wordlist_panel(node):
            for child in node.getElementsByTagName("include"):
                if child.getAttribute("file") == "word_suggestions.xml":
                    return True
            return node.getAttribute("id") == "wordlistbar"

        def visit(node, svg_filename):
            for child in node.childNodes:
                if child.nodeType != minidom.Node.ELEMENT_NODE or \
                   child.tagName not in ("panel", "box"):
                    continue
                fn = child.getAttribute("filename") or svg_filename
                if is_wordlist_panel(child) and \
                   not child.getElementsByTagName("key"):
                    doc.word_suggestions = True
                    continue
                layer_name = child.getAttribute("layer")
                if layer_name:
                    layer = None
                    for l in doc.layers:
                        if l.name.lower() == layer_name.lower():
                            layer = l
                    if not layer:
                        layer = EditorLayer(layer_name.capitalize())
                        doc.layers.append(layer)
                    collect_keys(child, fn, layer)
                visit(child, fn)

        visit(dom, None)

        # Keys outside of layer panels are visible on all layers;
        # the editor puts them on the first layer.
        common = EditorLayer("Main")
        for child in dom.childNodes:
            if child.nodeType == minidom.Node.ELEMENT_NODE and \
               child.tagName in ("panel", "box") and \
               not is_wordlist_panel(child):
                collect_keys(child, child.getAttribute("filename") or None,
                             common)
        if common.keys:
            if not doc.layers:
                doc.layers.append(common)
            else:
                for key in common.keys:
                    if not doc.layers[0].find_key(key.id):
                        doc.layers[0].keys.append(key)

        if not doc.layers:
            doc.layers.append(EditorLayer("Alpha"))

        _normalize_origin(doc)
        doc.filename = filename
        return doc

    # ------------------------------------------------------------------
    # Presets

    @classmethod
    def new_grid(cls, rows, columns, id="MyKeyboard"):
        """ Rows x columns of blank keys typing letters. """
        doc = cls(id)
        layer = doc.layers[0]
        chars = "abcdefghijklmnopqrstuvwxyz0123456789"
        for r in range(rows):
            for c in range(columns):
                i = r * columns + c
                key = layer_key(layer, c, r)
                if i < len(chars):
                    key.attributes["char"] = chars[i]
        return doc

    @classmethod
    def new_qwerty(cls, id="MyKeyboard", numbers_layer=True):
        """ Compact typewriter layout following the system keymap. """
        doc = cls(id)
        alpha = doc.layers[0]
        widths = {"TAB": 1.5, "CAPS": 1.75, "LFSH": 2.25, "RTSH": 2.75,
                  "BKSP": 2.0, "RTRN": 2.25, "BKSL": 1.5}
        rows = [
            [k.split(":")[0] for k in QWERTY_ROWS[0]] + ["BKSP"],
            ["TAB"] + [k.split(":")[0] for k in QWERTY_ROWS[1]] + ["BKSL"],
            ["CAPS"] + [k.split(":")[0] for k in QWERTY_ROWS[2][:-1]] +
            ["RTRN"],
            ["LFSH"] + [k.split(":")[0] for k in QWERTY_ROWS[3]] + ["RTSH"],
        ]
        for r, row in enumerate(rows):
            x = 0.0
            for id in row:
                w = widths.get(id, 1.0)
                key = layer_key(alpha, x, r, w, id=id)
                key.attributes["group"] = "alphanumeric" \
                    if id in US_LABELS else "bottomrow"
                x += w

        bottom = [("LCTL", 1.5), ("LALT", 1.25)]
        if numbers_layer:
            bottom.append(("layer1", 1.25))
        bottom += [("SPCE", 6.5 if numbers_layer else 7.75), ("RALT", 1.25),
                   ("LEFT", 1.0), ("RGHT", 1.0), ("hide", 1.25)]
        _add_row(alpha, bottom, 4)

        if numbers_layer:
            numbers = EditorLayer("Numbers")
            doc.layers.append(numbers)
            keypad = ["789", "456", "123"]
            for r, row in enumerate(keypad):
                for c, ch in enumerate(row):
                    key = layer_key(numbers, 4 + c, r, id="n" + ch)
                    key.attributes["char"] = ch
                    key.attributes["group"] = "alphanumeric"
            for r, (id, ch) in enumerate([("plus", "+"), ("minus", "-"),
                                          ("times", "*"), ("divide", "/")]):
                key = layer_key(numbers, 7.25, r, id=id)
                key.attributes["char"] = ch
                key.attributes["group"] = "alphanumeric"
            zero = layer_key(numbers, 4, 3, 2, id="n0")
            zero.attributes["char"] = "0"
            zero.attributes["group"] = "alphanumeric"
            dot = layer_key(numbers, 6, 3, id="dot")
            dot.attributes["char"] = "."
            dot.attributes["group"] = "alphanumeric"
            _add_row(numbers, [("BKSP", 2.0)], 0, x=8.5)
            _add_row(numbers, [("RTRN", 2.0)], 1, x=8.5)
            _add_row(numbers, [("LCTL", 1.5), ("LALT", 1.25),
                               ("layer0", 1.25), ("SPCE", 6.5),
                               ("LEFT", 1.0), ("RGHT", 1.0),
                               ("hide", 1.25)], 4)
            numbers.find_key("layer0").attributes["label"] = "Abc"
            alpha.find_key("layer1").attributes["label"] = "123"
        return doc


def layer_key(layer, col, row, width=1.0, height=1.0, id=None):
    """ Append a key at grid position (col, row) in key units. """
    key = EditorKey(id or layer.new_key_id(),
                    col * KEY_UNIT + KEY_GAP / 2,
                    row * KEY_UNIT + KEY_GAP / 2,
                    width * KEY_UNIT - KEY_GAP,
                    height * KEY_UNIT - KEY_GAP)
    if LAYER_KEY_PATTERN.match(key.id):
        key.attributes["button"] = "true"
    layer.keys.append(key)
    return key


def _add_row(layer, ids_widths, row, x=0.0):
    for id, w in ids_widths:
        key = layer_key(layer, x, row, w, id=id)
        key.attributes["group"] = "bottomrow"
        x += w


def snap(value, step=SNAP):
    return round(value / step) * step


def _normalize_origin(doc):
    """ Move imported keys so the keyboard starts at the origin. """
    keys = [k for layer in doc.layers for k in layer.keys]
    if not keys:
        return
    dx = min(k.x for k in keys) - KEY_GAP / 2
    dy = min(k.y for k in keys) - KEY_GAP / 2
    for k in keys:
        k.x -= dx
        k.y -= dy


def _safe_name(name):
    return re.sub(r"[^\w\-]+", "-", name).strip("-") or "Layer"


def _key_fill(key):
    type = key.get_action_type()
    if type == "layer":
        return "#cdad72"
    if type == "template" and key.id not in US_LABELS:
        return "#a97679"
    return "#d8d8d8"


def _svg_rect(id, x, y, w, h, fill):
    return ("  <rect id={} x='{:g}' y='{:g}' width='{:g}' height='{:g}' "
            "style='fill:{};stroke:#000000;stroke-width:1'/>"
            .format(quoteattr(id), x, y, w, h, fill))


def _svg_document(width, height, rects):
    return "\n".join(
        ["<?xml version='1.0' encoding='UTF-8' standalone='no'?>",
         "<!-- Created with the Onboard layout editor -->",
         "<svg xmlns='http://www.w3.org/2000/svg' version='1.1' "
         "width='{0:g}' height='{1:g}' viewBox='0 0 {0:g} {1:g}'>"
         .format(width, height)] +
        rects + ["</svg>", ""])


def _read_svg_rects(path):
    """ {id: (x, y, w, h)} of all rects and paths with an id. """
    from Onboard.KeyCommon import KeyPath   # lazy, needs Onboard deps
    result = {}
    dom = minidom.parse(path).documentElement
    for tag in ("rect", "path"):
        for node in dom.getElementsByTagName(tag):
            id = node.getAttribute("id")
            if not id:
                continue
            try:
                if tag == "rect":
                    result[id] = tuple(float(node.getAttribute(a))
                                       for a in ("x", "y", "width",
                                                 "height"))
                else:
                    r = KeyPath.from_svg_path(node.getAttribute("d")) \
                        .get_bounds()
                    result[id] = (r.x, r.y, r.w, r.h)
            except ValueError:
                continue
    return result


def _write_text(path, text):
    with open(path, "w", encoding="UTF-8") as f:
        f.write(text)
