"""Brand voice per brand, read from its brand manual (PDF)."""

import unittest

import pymupdf

from models.compliance.brand_voice_model import BrandColor, BrandTypeface, BrandVoiceIn
from services.compliance.evaluation import brand_voice_service as svc


def sample_manual() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 160), 0)
    logo.set_rect(logo.irect, (0, 82, 155))
    page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=logo)
    page.insert_text((72, 240), "Our voice is practical and plain-spoken.", fontname="helv")
    page2 = doc.new_page()
    page2.insert_text((72, 100), "Sky Blue HEX #00529B primary. Signal Orange #f28c28 accents.", fontname="tiro")
    page2.insert_text((72, 120), "Not a colour: order 123456 or ref A1B2C3D4.", fontname="tiro")
    data = doc.tobytes()
    doc.close()
    return data


class ReadPdfTests(unittest.TestCase):
    def setUp(self):
        self.facts = svc.read_pdf(sample_manual())

    def test_colour_codes_it_prints(self):
        self.assertEqual(self.facts.hexes, ["#00529B", "#F28C28"])

    def test_fonts_it_is_set_in(self):
        self.assertIn("Helvetica", self.facts.fonts)

    def test_images_that_could_be_the_logo(self):
        self.assertEqual([(p, w, h) for p, w, h, _, _ in self.facts.images], [(1, 160, 160)])
        self.assertEqual(svc._logo_guess(self.facts), 0)

    def test_no_images_no_logo_guess(self):
        self.assertIsNone(svc._logo_guess(svc.PdfFacts()))


class ContextEntryTests(unittest.TestCase):
    def test_the_analysis_gets_voice_tone_colours_typefaces_and_logo_rules(self):
        body = svc.context_body(BrandVoiceIn(
            voice="Practical and plain-spoken.", tone=["confident", "clear"],
            colors=[BrandColor(name="Sky Blue", hex="#00529B", usage="headings")],
            typefaces=[BrandTypeface(name="Montserrat", role="Headings")], logo_notes="Never stretch it.",
        ))
        self.assertEqual(body, "Practical and plain-spoken.\nTone: confident, clear.\n"
                               "Brand colours: Sky Blue #00529B (headings).\nTypefaces: Montserrat (Headings).\n"
                               "Logo: Never stretch it.")

    def test_an_empty_voice_gives_no_entry(self):
        self.assertEqual(svc.context_body(BrandVoiceIn()), "")


class CleanTests(unittest.TestCase):
    def test_trims_dedupes_and_uppercases(self):
        clean = svc._clean(BrandVoiceIn(
            voice="  Calm. ", tone=["calm", " calm ", ""],
            colors=[BrandColor(hex="#00529b"), BrandColor(hex="#00529B", name="dup")],
            typefaces=[BrandTypeface(name=" Inter ", role=" ")],
        ))
        self.assertEqual(clean.voice, "Calm.")
        self.assertEqual(clean.tone, ["calm"])
        self.assertEqual([c.hex for c in clean.colors], ["#00529B"])
        self.assertEqual((clean.typefaces[0].name, clean.typefaces[0].role), ("Inter", None))

    def test_hex_parsing(self):
        self.assertEqual(svc._hex("00529b"), "#00529B")
        self.assertIsNone(svc._hex("blue"))


if __name__ == "__main__":
    unittest.main()
