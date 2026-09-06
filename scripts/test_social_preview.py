"""Regression checks for the public preview, without starting the pipeline."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from flask import Flask, render_template
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard import social_preview as preview

STATIC = ROOT / "dashboard/static"
HONOR = json.loads((STATIC / "championship-honors.json").read_text())["worldChampion"]
CHAMPION = {"PLAYER_NAME": "Andrew Hedrick", "ATP_SCORE": 370.29998779296875,
            "WIN_RATE_PCT": 80.9000015258789, "TOP8S": 3}


class SocialPreviewTest(unittest.TestCase):
    def render_labels(self, row, honor=HONOR):
        labels = []
        original = ImageDraw.ImageDraw.text

        def record(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original(draw, xy, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            data = preview.render_social_preview(row, STATIC, honor)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (1200, 630))
            self.assertEqual(image.format, "PNG")
        return labels

    def test_database_float_precision_is_not_drawn(self):
        labels = self.render_labels(CHAMPION)
        self.assertIn("80.9%", labels)
        self.assertIn("370.3", labels)
        self.assertFalse(any("900001" in label for label in labels))
        self.assertTrue(any("2026 world champion" in label for label in labels))

    def test_zero_missing_and_independent_honors(self):
        labels = self.render_labels({**CHAMPION, "PLAYER_NAME": "Another Player",
                                    "ATP_SCORE": 0, "WIN_RATE_PCT": 0, "TOP8S": 0})
        self.assertIn("0.0%", labels)
        self.assertIn("0.0", labels)
        self.assertIn("0", labels)
        self.assertFalse(any("world champion" in label for label in labels))
        self.assertIn("the league awaits.", self.render_labels(None))
        self.assertEqual(preview.format_number(float("nan")), "—")
        self.assertEqual(preview.format_number(float("inf")), "—")

    def test_every_drawn_label_fits_its_box_with_long_data(self):
        original = preview._text

        def checked(draw, text, box, static_dir, size, *args, **kwargs):
            fitted, font, bounds = preview.fit_text(draw, text, box, static_dir, size,
                                                    heavy=kwargs.get("heavy", True))
            self.assertLessEqual(bounds[2] - bounds[0], box[2] - box[0])
            self.assertLessEqual(bounds[3] - bounds[1], box[3] - box[1])
            return original(draw, text, box, static_dir, size, *args, **kwargs)

        with patch.object(preview, "_text", checked):
            self.render_labels({**CHAMPION, "PLAYER_NAME": "A very long player name " * 12,
                                "TOP_DECK_NAME": "a long archetype name " * 12,
                                "ATP_SCORE": 1e30})

    def test_metadata_uses_versioned_https_even_behind_http_proxy(self):
        app = Flask(__name__, template_folder=str(ROOT / "dashboard/templates"))
        with app.test_request_context("/", base_url="http://indigocircuit.app"):
            html = render_template("league.html")
        expected = 'content="https://indigocircuit.app/og-image.png?v=ghost-woods-2"'
        self.assertEqual(html.count(expected), 2)
        self.assertNotIn('content="http://indigocircuit.app/og-image', html)


if __name__ == "__main__":
    unittest.main()
