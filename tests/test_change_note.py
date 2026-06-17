"""Inline "what changed" note formatting — pure, no network."""
from agent.prompts import format_change_note


def test_empty_changes_returns_empty_string():
    assert format_change_note([]) == ""


def test_old_new_values_rendered_with_effective_date():
    note = format_change_note(
        [
            {
                "title": "Construction use tax rate increased",
                "old_value": "3.00%",
                "new_value": "3.46%",
                "effective_date": "2026-01-01",
            }
        ]
    )
    assert "3.00%" in note
    assert "**3.46%**" in note
    assert "2026-01-01" in note
    assert note.startswith("📋 Heads up")


def test_change_without_values_still_notes_effective_date():
    note = format_change_note(
        [{"title": "Renewals move online", "old_value": None, "new_value": None, "effective_date": "2026-01-01"}]
    )
    assert "Renewals move online" in note
    assert "2026-01-01" in note


def test_caps_number_of_items():
    changes = [
        {"title": f"Change {i}", "old_value": "$1", "new_value": "$2", "effective_date": "2026-01-01"}
        for i in range(5)
    ]
    note = format_change_note(changes, max_items=2)
    assert note.count("→") == 2
