"""Small-text recovery preserves full-image meaning and never guesses missing words."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from PIL import Image
from services.compliance.media import image_small_text as detail
from services.compliance.media.image_finding_zoom import sections


def region(text="SMALL LABEL", confidence=.95, crop=1, box=None):
    return {"crop":crop,"text":text,"confidence":confidence,"box":box or [300,100,350,400]}


def image_bytes():
    stream=io.BytesIO()
    Image.new("RGB",(620,618),"white").save(stream,format="PNG")
    return stream.getvalue()


class MergeTests(unittest.TestCase):
    def test_new_word_is_retained_without_replacing_existing_qualifications(self):
        original="Conceptual illustration. Not approved hardware."
        merged=detail.merge(original,[region("SMALL LABEL")],4)
        self.assertTrue(merged.startswith(original))
        self.assertIn("SMALL LABEL",merged)

    def test_overlap_duplicates_and_already_read_words_are_not_repeated(self):
        original="Cooling for transport."
        merged=detail.merge(original,[region("Cooling for transport."),region("MAFIA"),region("mafia",crop=2)],4)
        self.assertEqual(merged,original+"\nMAFIA")

    def test_ambiguous_text_is_not_added(self):
        self.assertEqual(detail.merge("Caption",[region("maybe something",confidence=.5)],4),"Caption")

    def test_blank_crop_does_not_invent_a_finding(self):
        self.assertEqual(detail.merge("Caption",[],4),"Caption")

    def test_invalid_region_does_not_silently_become_complete(self):
        for invalid in (region(crop=5),region(crop=True),region(confidence=float("nan")),region(box=[0,0,1001,10])):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                detail.merge("Caption",[invalid],4)

    def test_focused_region_is_enlarged_without_synthesizing_pixels(self):
        tiles=detail.focused_sections(image_bytes(),[{"box":[380,100,420,160]}])
        self.assertEqual(len(tiles),1)
        with Image.open(io.BytesIO(tiles[0][0])) as crop:
            self.assertGreater(crop.width,500)
        self.assertEqual(detail.focused_sections(image_bytes(),[{"box":[0,0,1000,1000]}]),[])

    def test_excess_text_is_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            detail.merge("a"*5000,[region("MORE WORDS")],4)

    def test_crops_cover_original_with_overlap_and_are_enlarged(self):
        tiles=sections(image_bytes())
        self.assertEqual(len(tiles),4)
        self.assertLess(tiles[2][1][0],tiles[0][1][2])
        self.assertLess(tiles[1][1][1],tiles[0][1][3])
        for encoded,bounds,size in tiles:
            self.assertEqual(size,(620,618))
            with Image.open(io.BytesIO(encoded)) as tile:
                self.assertGreater(tile.width,bounds[2]-bounds[0])


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_batched_call_reads_all_crops_without_a_target_word_prompt(self):
        generate=AsyncMock(return_value=SimpleNamespace(text=json.dumps({"regions":[region("SMALL LABEL")]})))
        text=await detail.read(generate,image_bytes(),"Original caption")
        self.assertEqual(text.text,"Original caption\nSMALL LABEL")
        self.assertEqual(text.regions[0]["text"],"SMALL LABEL")
        self.assertEqual(generate.await_count,1)
        kwargs=generate.await_args.kwargs
        self.assertEqual(sum(not isinstance(x,str) for x in kwargs["contents"]),4)
        self.assertNotIn("MAFIA",kwargs["contents"][0])

    async def test_uncertain_word_is_preserved_as_evidence_not_asserted_text(self):
        generate=AsyncMock(return_value=SimpleNamespace(text=json.dumps({"regions":[region("MAFIA",confidence=.6)]})))
        read=await detail.read(generate,image_bytes(),"Original caption")
        self.assertEqual(read.text,"Original caption")
        self.assertEqual(read.regions[0]["text"],"MAFIA")
        self.assertEqual(read.regions[0]["confidence"],.6)

    async def test_incomplete_output_is_bounded_and_not_saved_as_success(self):
        generate=AsyncMock(return_value=SimpleNamespace(text='{"bad":true}'))
        with self.assertRaises(ValueError):
            await detail.read(generate,image_bytes(),"Original caption")
        self.assertEqual(generate.await_count,2)
