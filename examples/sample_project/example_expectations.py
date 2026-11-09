from pipeline import normalize_label, render_label


def test_normalize_label_preserves_display_case():
    assert normalize_label("  Octane  ") == "Octane"


def test_render_label():
    assert render_label("Octane") == "Octane (6)"
