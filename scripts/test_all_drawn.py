"""Regression checks for the blank-board contract and pixel sample gate."""
from __future__ import annotations

import base64
import copy
import io
import unittest

from PIL import Image

from check_all_drawn import inspect_structure, inspect_pixels
from validate import validate_document


def project():
    return {'durationMs': 2000, 'fps': 30, 'canvas': {'width': 200, 'height': 100},
            'theme': {'paper': '#ffffff', 'dotSpacing': 0},
            'drawPolicy': {'profile': 'all-drawn'}, 'toolMode': 'serial', 'elements': [], 'cues': [],
            'scenes': [{'id': 'one', 'startMs': 0, 'endMs': 2000, 'elements': [
                {'id': 'object', 'type': 'paint', 'd': 'M10 10H80V60H10Z',
                 'startMs': 100, 'durationMs': 900, 'color': '#ff9900'}]}],
            'toolTrack': [{'sceneId': 'one', 'elementId': 'object', 'startMs': 100, 'endMs': 1000}]}


class FakePage:
    def __init__(self, painter):
        self.painter = painter

    def evaluate(self, script, ms=None):
        if ms is None:
            return True
        image = self.painter(ms)
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        return 'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode()


class AllDrawnTests(unittest.TestCase):
    def test_positive_paint_and_matching_single_track(self):
        doc = project()
        self.assertTrue(inspect_structure(doc)['passed'])
        self.assertTrue(validate_document(doc, scheduled=True)['valid'])

    def test_old_projects_require_explicit_legacy_profile(self):
        doc = project()
        doc['drawPolicy'] = {'profile': 'legacy'}
        doc['scenes'][0]['elements'][0].update(instant=True, durationMs=0)
        doc['toolTrack'] = []
        self.assertTrue(validate_document(doc, scheduled=True)['valid'])
        self.assertFalse(inspect_structure(doc)['structureChecked'])

    def test_omitted_profile_enforces_all_drawn_in_direct_validation(self):
        doc = project()
        doc.pop('drawPolicy')
        self.assertTrue(validate_document(doc, scheduled=True)['valid'])
        self.assertEqual(inspect_structure(doc)['profile'], 'all-drawn')
        self.assertTrue(inspect_structure(doc)['structureChecked'])
        doc['scenes'][0]['elements'][0].update(instant=True, durationMs=0)
        doc['toolTrack'] = []
        for scheduled in (False, True):
            with self.assertRaises(ValueError):
                validate_document(doc, scheduled=scheduled)
        self.assertFalse(inspect_structure(doc)['passed'])

    def test_only_explicit_legacy_can_disable_drawing_policy(self):
        doc = project()
        for policy in ({}, {'profile': 'unknown'}, {'profile': None}, None):
            doc['drawPolicy'] = policy
            if policy == {}:
                self.assertTrue(inspect_structure(doc)['structureChecked'])
            else:
                with self.assertRaises(ValueError):
                    validate_document(doc, scheduled=True)

    def test_instant_and_zero_duration_are_rejected(self):
        for changes in ({'instant': True}, {'durationMs': 0}, {'passive': True}):
            doc = project()
            doc['scenes'][0]['elements'][0].update(changes)
            self.assertFalse(inspect_structure(doc)['passed'])
            with self.assertRaises(ValueError):
                validate_document(doc, scheduled=True)

    def test_all_images_must_use_swept_draw_mode(self):
        for mode, passed in [('fade', False), ('sketch', False), (None, False), ('draw', True)]:
            doc = project()
            doc['scenes'][0]['elements'][0].update(type='image', mode=mode,
                src='a.png', inkSrc='a-ink.png', traceSrc='a.json', x=10, y=10, width=50)
            self.assertEqual(inspect_structure(doc)['passed'], passed)
            if passed:
                self.assertTrue(validate_document(doc, scheduled=True)['valid'])

    def test_auto_captions_global_art_fill_and_scene_wipes_are_rejected(self):
        variants = [({'cues': [{'startMs': 0, 'endMs': 1000, 'text': 'caption'}]}),
                    {'elements': [{'id': 'global-art'}]},
                    {'transition': {'durationMs': 100}},
                    {'theme': {'paper': '#fdfcf3', 'dotSpacing': 0}},
                    {'theme': {'paper': '#fff', 'dotSpacing': 30}}]
        for changes in variants:
            doc = project()
            doc.update(changes)
            self.assertFalse(inspect_structure(doc)['passed'])
        doc = project()
        doc['scenes'][0]['elements'][0]['type'] = 'fill'
        self.assertFalse(inspect_structure(doc)['passed'])

    def test_tool_track_must_cover_each_complete_interval(self):
        for changes in ({'startMs': 200}, {'endMs': 900}):
            doc = project()
            doc['toolTrack'][0].update(changes)
            self.assertFalse(inspect_structure(doc)['passed'])
        doc = project()
        doc['toolTrack'] = []
        self.assertFalse(inspect_structure(doc)['passed'])

    def test_nonintegral_scene_start_keeps_first_exported_frame_blank(self):
        doc = project()
        doc['scenes'][0]['startMs'] = 101
        doc['scenes'][0]['elements'][0]['startMs'] = 1
        self.assertFalse(inspect_structure(doc, scheduled=False)['passed'])
        doc['scenes'][0]['elements'][0]['startMs'] = 33
        self.assertTrue(inspect_structure(doc, scheduled=False)['passed'])

    def test_drawn_object_can_be_erased_after_its_drawing_finishes(self):
        doc = project()
        doc['scenes'][0]['elements'].append({'id': 'correction', 'type': 'erase',
            'targets': ['object'], 'region': {'x': 10, 'y': 10, 'width': 70, 'height': 50},
            'tool': 'eraser', 'startMs': 1200, 'durationMs': 600})
        doc['toolTrack'].append({'sceneId': 'one', 'elementId': 'correction',
                                'startMs': 1200, 'endMs': 1800})
        self.assertTrue(inspect_structure(doc)['passed'])
        self.assertTrue(validate_document(doc, scheduled=True)['valid'])

    def test_erasing_an_unfinished_or_missing_object_is_rejected(self):
        for targets in (['object'], ['unknown-object']):
            doc = project()
            doc['scenes'][0]['elements'].append({'id': 'correction', 'type': 'erase',
                'targets': targets, 'region': {'x': 10, 'y': 10, 'width': 70, 'height': 50},
                'tool': 'eraser', 'startMs': 500, 'durationMs': 600})
            # Unscheduled author validation must also reject an eraser that
            # begins before its target has been drawn completely.
            self.assertFalse(inspect_structure(doc, scheduled=False)['passed'])
            with self.assertRaises(ValueError):
                validate_document(doc, scheduled=False)

    def test_pixel_sampler_accepts_color_overpainting(self):
        def paint(ms):
            image = Image.new('RGB', (200, 100), 'white')
            if ms > 100:
                color = 'red' if ms < 900 else 'green'
                image.paste(color, (10, 10, 50, 50))
            return image
        result = inspect_pixels(FakePage(paint), project())
        self.assertTrue(result['passed'])
        self.assertTrue(result['pixelChecked'])
        self.assertEqual(result['sceneStartChecks'][0]['nonWhitePixels'], 0)

    def test_pixel_sampler_catches_preexisting_board_content(self):
        result = inspect_pixels(FakePage(lambda ms: Image.new('RGB', (200, 100), 'red')), project())
        self.assertFalse(result['passed'])
        self.assertGreater(result['sceneStartChecks'][0]['nonWhitePixels'], 0)

    def test_pixel_sampler_catches_start_snap_and_no_midway_growth(self):
        def snap(ms):
            image = Image.new('RGB', (200, 100), 'white')
            if ms >= 100:
                image.paste('black', (10, 10, 50, 50))
            return image
        result = inspect_pixels(FakePage(snap), project())
        self.assertFalse(result['passed'])
        self.assertGreater(result['actionProgressSamples'][0]['startPopPixels'], 0)
        self.assertEqual(result['actionProgressSamples'][0]['secondHalfChangedPixels'], 0)


if __name__ == '__main__':
    unittest.main()
