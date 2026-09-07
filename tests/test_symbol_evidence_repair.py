from agents.ratsnestpro.symbol_evidence_repair import table_pin_rows


def test_package_column_is_not_inferred_from_physical_order():
    text = "LQFP32 LQFP48 LQFP64"
    layout = "19  29  37  PA9  I/O  FT_fd\n21  32  42  PA10  I/O  FT_fd"
    assert table_pin_rows(layout, text, "lqfp64") == [
        {"number": "37", "name": "PA9", "type": "bidirectional"},
        {"number": "42", "name": "PA10", "type": "bidirectional"},
    ]
    assert table_pin_rows(layout, text, "lqfp48")[0]["number"] == "29"
    assert table_pin_rows(layout, text, "qfn64") == []


def test_ambiguous_or_incomplete_rows_do_not_authorize_changes():
    assert table_pin_rows("19  37  PA9  I/O", "LQFP32 LQFP48 LQFP64", "lqfp64") == []
    assert table_pin_rows("19  29  37  PA9  unknown", "LQFP32 LQFP48 LQFP64", "lqfp64") == []
