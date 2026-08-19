from app.consolidation import consolidate_records


def test_same_rr_same_description_is_summed():
    rows = [
        {"rr_reference_no":"RR-1","description":"OFFICE CHAIR","kilos":"100","net_weight":"90","qty_pcs":"10","less_cage_or_pallets":"10","uom":"PCS","source_page_number":1,"source_table_row_number":4},
        {"rr_reference_no":"RR-1","description":"Office   Chair","kilos":"50","net_weight":"45","qty_pcs":"5","less_cage_or_pallets":"5","uom":"PCS","source_page_number":1,"source_table_row_number":7},
    ]
    result = consolidate_records(rows)
    assert len(result) == 1
    assert result[0]["kilos"] == "150"
    assert result[0]["net_weight"] == "135"
    assert result[0]["qty_pcs"] == "15"
    assert result[0]["occurrence_count"] == 2


def test_same_description_different_rr_stays_separate():
    rows = [
        {"rr_reference_no":"RR-1","description":"OFFICE CHAIR","kilos":"100"},
        {"rr_reference_no":"RR-2","description":"OFFICE CHAIR","kilos":"50"},
    ]
    result = consolidate_records(rows)
    assert len(result) == 2
