#!/usr/bin/python3

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

import os
import glob
import tempfile
import unittest

import Onboard.LayoutLoaderSVG
from Onboard.LayoutLoaderSVG import LayoutLoaderSVG
from Onboard.LayoutEditorModel import EditorDocument, EditorKey
import Onboard.osk as osk
from Onboard.utils import Translation

LAYOUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "layouts")


class TestLayoutEditorModel(unittest.TestCase):

    class Config_mockup:
        class ThemeSettings:
            key_label_overrides = {}
        theme_settings = ThemeSettings()

        def find_layout_filename(self, filename, description,
                                 *args, **kwargs):
            return os.path.join(LAYOUTS_DIR, filename)

    @classmethod
    def setUpClass(cls):
        # A second Virtkey instance in one process crashes on Wayland.
        cls._vk = osk.Virtkey()

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="test_onboard_")
        self._dir = self._tmp_dir.name
        Translation.install("onboard")

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_presets_load_in_onboard(self):
        """ Every key the editor writes must be found by Onboard. """
        for doc in (EditorDocument.new_qwerty("Q"),
                    EditorDocument.new_grid(3, 5, "G")):
            doc.word_suggestions = True
            self.assertEqual([], doc.validate())
            fn = os.path.join(self._dir, doc.id + ".onboard")
            doc.save(fn)

            layout = self._load_with_onboard(fn)
            ids = {key.id for key in layout.iter_keys()}
            for layer in doc.layers:
                for key in layer.keys:
                    self.assertIn(key.id, ids)
            self.assertIn("wordlist", ids)

    def test_round_trip(self):
        doc = EditorDocument.new_qwerty("RoundTrip")
        key = EditorKey("k1", 1, 101, 38, 18,
                        {"char": "€", "label": "Euro & co",
                         "tooltip": "a 'quoted' \"value\""})
        doc.layers[1].keys.append(key)
        fn = os.path.join(self._dir, "RoundTrip.onboard")
        doc.save(fn)

        loaded = EditorDocument.load(fn)
        self.assertFalse(loaded.imported)
        self.assertEqual(doc.build_layout_xml(), loaded.build_layout_xml())
        k = loaded.layers[1].find_key("k1")
        self.assertEqual((1, 101, 38, 18), (k.x, k.y, k.w, k.h))
        self.assertEqual(key.attributes, k.attributes)

    def test_import_system_layouts(self):
        for fn in glob.glob(os.path.join(LAYOUTS_DIR, "*.onboard")):
            doc = EditorDocument.load(fn)
            self.assertTrue(doc.imported, fn)
            self.assertTrue(any(layer.keys for layer in doc.layers), fn)

    def test_validate(self):
        doc = EditorDocument.new_grid(1, 2)
        doc.layers[0].keys[1].id = doc.layers[0].keys[0].id
        doc.layers[0].keys.append(EditorKey("layer3"))
        problems = doc.validate()
        self.assertEqual(2, len(problems))

    def test_action_types(self):
        key = EditorKey("k1", attributes={"char": "a"})
        self.assertEqual("char", key.get_action_type())
        key.set_action("keycode", "38")
        self.assertEqual({"keycode": "38"}, key.attributes)
        self.assertEqual("template", EditorKey("LFSH").get_action_type())
        self.assertEqual("layer", EditorKey("layer2").get_action_type())
        self.assertEqual("none", EditorKey("k2").get_action_type())

    def _load_with_onboard(self, filename):
        loader = LayoutLoaderSVG()
        loader._get_system_keyboard_layout = \
            lambda vk: ("us", "", "pc105")
        Onboard.LayoutLoaderSVG.config = self.Config_mockup()
        return loader.load(self._vk, filename, None)
