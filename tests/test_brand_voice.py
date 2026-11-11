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


class TallPageTests(unittest.TestCase):
    """A one-page manual drawn as one long strip, with the logo as vector shapes (no image file)."""

    def setUp(self):
        doc = pymupdf.open()
        page = doc.new_page(width=1000, height=4000)
        page.draw_rect(pymupdf.Rect(0, 0, 1000, 4000), color=None, fill=(1, 1, 1))
        page.draw_rect(pymupdf.Rect(100, 3000, 400, 3100), color=None, fill=(0, 0.35, 0.5))  # the "logo"
        page.insert_text((100, 200), "Brand Manual", fontname="helv")
        self.data = doc.tobytes()
        doc.close()
        self.facts = svc.read_pdf(self.data)

    def test_the_page_is_shown_in_sections_that_cover_it_all(self):
        spans = [(round(y0), round(y1)) for _, _, y0, _, y1, _ in self.facts.sections]
        self.assertGreater(len(spans), 2)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], 4000)
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLess(b0, a1)  # overlapping, so nothing is cut in half

    def test_a_drawn_logo_is_cut_out_where_the_ai_found_it(self):
        section = next(s for s in self.facts.sections if s[2] <= 3000 and s[4] >= 3100)
        _, x0, y0, x1, y1, _ = section
        box = [int((3000 - y0) / (y1 - y0) * 1000), 100, int((3100 - y0) / (y1 - y0) * 1000), 400]
        png = svc.crop_logo(self.data, section, box)
        self.assertTrue(png and png.startswith(bytes([0x89]) + b"PNG"))

    def test_a_box_too_small_gives_no_logo(self):
        self.assertIsNone(svc.crop_logo(self.data, self.facts.sections[0], [10, 10, 12, 12]))
        self.assertIsNone(svc.crop_logo(self.data, self.facts.sections[0], []))


class PaletteTests(unittest.TestCase):
    """Rows of swatches drawn in the manual: the tints, shades and other variants of each main colour."""

    def setUp(self):
        doc = pymupdf.open()
        page = doc.new_page(width=1000, height=600)
        page.insert_text((36, 90), "Brighten", fontname="helv")
        for i, hexv in enumerate(["#005981", "#1E5E81", "#366D8D", "#4D7D9B"]):
            rgb = tuple(int(hexv[k:k + 2], 16) / 255 for k in (1, 3, 5))
            page.draw_rect(pymupdf.Rect(36 + i * 100, 100, 136 + i * 100, 156), color=None, fill=rgb)
        page.insert_text((36, 290), "Darken", fontname="helv")
        for i, hexv in enumerate(["#ED2224", "#CE1C1E", "#B01618"]):
            rgb = tuple(int(hexv[k:k + 2], 16) / 255 for k in (1, 3, 5))
            page.draw_rect(pymupdf.Rect(36 + i * 100, 300, 136 + i * 100, 356), color=None, fill=rgb)
        # Two lone blocks aren't a palette row.
        page.draw_rect(pymupdf.Rect(36, 450, 136, 506), color=None, fill=(0, 0, 0))
        page.draw_rect(pymupdf.Rect(136, 450, 236, 506), color=None, fill=(1, 0, 0))
        self.data = doc.tobytes()
        doc.close()

    def test_rows_of_swatches_with_their_labels(self):
        rows = svc.read_pdf(self.data).swatch_rows
        self.assertEqual(rows, [("Tints", ["#005981", "#1E5E81", "#366D8D", "#4D7D9B"]),
                                ("Shades", ["#ED2224", "#CE1C1E", "#B01618"])])

    def test_each_row_goes_under_its_main_colour(self):
        palette = svc.build_palette(svc.read_pdf(self.data).swatch_rows, ["#005981", "#ED2224"])
        self.assertEqual([(p.base_hex, p.label, len(p.colors)) for p in palette],
                         [("#005981", "Tints", 4), ("#ED2224", "Shades", 3)])

    def test_a_row_not_starting_with_a_main_colour_goes_under_the_closest(self):
        palette = svc.build_palette([("Palette", ["#0A5A80", "#123456", "#654321"])], ["#005981", "#ED2224"])
        self.assertEqual(palette[0].base_hex, "#005981")

    def test_the_palette_overview_itself_is_left_out(self):
        self.assertEqual(svc.build_palette([("Palette", ["#005981", "#ED2224", "#939598"])],
                                           ["#005981", "#ED2224", "#939598"]), [])

    def test_the_analysis_hears_about_it_grouped_by_colour(self):
        voice = BrandVoiceIn(voice="Calm.", colors=[BrandColor(name="Grand Bleu", hex="#005981")])
        body = svc.context_body(voice, [svc.PaletteRow(base_hex="#005981", label="Tints", colors=["#005981"] * 1),
                                        svc.PaletteRow(base_hex="#005981", label="Shades", colors=["#000000", "#111111"])])
        self.assertIn("Extended palette (also on-brand): Grand Bleu: 1 tints, 2 shades.", body)


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
