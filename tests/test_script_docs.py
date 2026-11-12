"""Script documents: templates, colours per person, and when every reviewer has checked in."""

from uuid import uuid4

from services.compliance.evaluation import script_doc_service as svc
from services.compliance.evaluation.script_doc_templates import TEMPLATES, get_template, template_html


def test_the_blank_script_is_an_empty_page():
    assert [t.key for t in TEMPLATES] == ["blank"]
    assert template_html(get_template("blank"), "Title") == "<p></p>"
    assert get_template("nope").key == "blank"


def test_each_person_gets_their_own_colour():
    people = [uuid4() for _ in range(5)]
    assert len({svc._color(p, people) for p in people}) == 5


def _doc(status="IN_REVIEW"):
    return {"status": status}


def test_all_checked_needs_every_editing_reviewer_and_ignores_view_only():
    a = {"capacity": "EDIT", "vote": "APPROVE"}
    b = {"capacity": "EDIT", "vote": None}
    v = {"capacity": "VIEW", "vote": None}
    assert not svc._all_checked(_doc(), [a, b, v])
    b["vote"] = "DONE"
    assert svc._all_checked(_doc(), [a, b, v])
    assert not svc._all_checked(_doc("LEVEL2_REVIEW"), [a, b])
    assert not svc._all_checked(_doc(), [v])


def test_a_document_splits_into_its_scripts_by_heading():
    from services.compliance.evaluation.script_doc_split import split_document
    from services.compliance.evaluation.script_doc_templates import empty_script_html
    one = empty_script_html(1, "Twenty five years").replace("<p>Storyline:", "<p>Storyline:")
    two = one + "<h2>2. The pigeon</h2><p>Storyline: x</p><table><tbody><tr><td><p>0:00</p></td><td><p>A pigeon</p></td></tr></tbody></table>"
    parts = split_document(two, "Doc")
    assert [t for t, _h, _x in parts] == ["Twenty five years", "The pigeon"]
    assert "Visual / footage" in parts[0][2] and "A pigeon" in parts[1][2]
    assert split_document("<p>Just words</p>", "Doc")[0][0] == "Doc"
    assert split_document("", "Doc") == []


def test_an_uploaded_script_becomes_a_page_with_its_table():
    from services.compliance.evaluation.script_doc_templates import empty_script_html, text_to_html
    html = text_to_html("Pigeon", "| Time | Voiceover |\n| --- | --- |\n| 0:00 | **A pigeon** |\n\nHello there")
    assert html.startswith("<h2>Pigeon</h2>") and "<th><p>Time</p></th>" in html and "<td><p>A pigeon</p></td>" in html
    assert "<p>Hello there</p>" in html
    page = empty_script_html(3, "New script")
    assert page.count("<tr>") == 6 and "<h2>3. New script</h2>" in page
