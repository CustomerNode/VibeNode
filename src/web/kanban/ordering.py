"""Per-column sort strategies and gap-numbered reorder logic.

Position integers use a gap-numbering scheme (1000, 2000, 3000...) that
allows O(1) insertion between any two items by computing the midpoint.
When the gap collapses to zero (collision), the entire column is
renumbered in a single pass.
"""

POSITION_GAP = 1000


def calculate_position(after_pos, before_pos):
    """Calculate new position between two existing positions.

    If *before_pos* is None the item is placed at the end.
    """
    if before_pos is None:
        before_pos = after_pos + 2 * POSITION_GAP
    return (after_pos + before_pos) // 2


def needs_renumber(after_pos, before_pos, new_pos):
    """Return True when a midpoint collision means the column needs renumbering."""
    return new_pos == after_pos or new_pos == before_pos


def generate_positions(count):
    """Return *count* evenly-spaced positions for a full renumber."""
    return [(i + 1) * POSITION_GAP for i in range(count)]
