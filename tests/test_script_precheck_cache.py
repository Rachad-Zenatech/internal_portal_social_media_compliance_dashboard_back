import json

import pytest

from services.compliance.evaluation.script_precheck_cache import check_key, precheck_record, still_valid
from services.compliance.media.script_content import VERSION


def record(text="Voiceover: Original claim", **changes):
    check = {"score": 90, "rulebook_version": "rules-1", "script_scoring_version": VERSION, **changes}
    return precheck_record({"title": "Original title"}, text, check)


def test_unchanged_script_reuses_its_saved_check_across_detail_edits():
    check = still_valid(record(), {"title": "Updated title", "platforms": ["FACEBOOK"]}, "Voiceover: Original claim", "rules-1")
    assert check["score"] == 90


@pytest.mark.parametrize("changes", [{"script_scoring_version": "old"}, {"rulebook_version": "rules-2"}])
def test_older_policy_or_rulebook_requires_a_new_check(changes):
    assert still_valid(record(**changes), {}, "Voiceover: Original claim", "rules-1") is None


def test_changed_wording_or_sources_cannot_reuse_a_saved_check():
    assert still_valid(record(), {}, "Voiceover: Updated claim", "rules-1") is None
    text = "Voiceover: Original claim\nSources used: https://example.com/first"
    assert still_valid(record(text), {}, text.replace("/first", "/second"), "rules-1") is None


def test_saved_json_and_loaded_records_accept_equivalent_line_endings():
    text = "Voiceover:\nOriginal claim"
    saved = record(text)
    assert still_valid(json.loads(saved), {}, " Voiceover:\r\nOriginal claim ", "rules-1")["score"] == 90
    assert check_key({}, text) == check_key({}, " Voiceover:\r\nOriginal claim ")


def test_missing_check_or_policy_is_never_reused():
    assert still_valid(None, {}, "text", None) is None
    saved = json.loads(record())
    del saved["check"]["script_scoring_version"]
    assert still_valid(saved, {}, "Voiceover: Original claim", "rules-1") is None


def test_malformed_saved_json_requires_a_new_check_instead_of_crashing():
    assert still_valid("{broken", {}, "text", None) is None
