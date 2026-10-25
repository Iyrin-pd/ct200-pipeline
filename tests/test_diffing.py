from tests.helpers import doc, node
from app.diffing import diff_summary


def d(old, new, h="H", h2=None):
    return diff_summary(h, old, h2 or h, new)


def test_identical_after_normalisation_is_none():
    assert d("low\u2011battery  icon", "low-battery icon")["change_class"] == "none"


def test_case_and_punctuation_only_is_cosmetic():
    assert d("Hello, world.", "hello world")["change_class"] == "cosmetic"


def test_synonym_rewrite_is_wording_not_material():
    r = d("The unit is small and light.", "The device is tiny and light.")
    assert r["change_class"] == "wording" and r["numeric_changes"] == []


def test_one_changed_pressure_threshold_is_material():
    r = d("Hypertensive crisis: systolic > 180", "Hypertensive crisis: systolic > 190")
    assert r["change_class"] == "material" and r["numeric_changes"][0]["old"].startswith("180")


def test_negation_flip_is_material_even_with_no_numbers():
    assert d("Do not use alcohol.", "Do use alcohol.")["change_class"] == "material"


def test_real_changes_v1_v2():
    a, b = doc(1), doc(2)
    expect = {"2.1.1.1": "300", "3.2": "40", "4.2": "2 seconds", "4.3": "E1\u2013E5"}
    for num, old_frag in expect.items():
        x, y = node(a, num), node(b, num)
        r = diff_summary(x.heading, x.body_text, y.heading, y.body_text)
        assert r["change_class"] == "material", num
        assert any(old_frag in (c["old"] or "") for c in r["numeric_changes"]), (num, r["numeric_changes"])
    for num in ("1.1", "2.1", "3.3", "7.1", "8.1"):
        x, y = node(a, num), node(b, num)
        assert diff_summary(x.heading, x.body_text, y.heading, y.body_text)["change_class"] == "none", num


def test_heading_rename_alone_is_reported():
    r = d("same", "same", "Alarm Thresholds", "Alarm Settings")
    assert r["heading_changed"] and r["change_class"] == "wording"