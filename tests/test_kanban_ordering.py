"""Tests for app.kanban.ordering — gap-numbered position calculations."""

from web.kanban.ordering import (
    calculate_position,
    needs_renumber,
    generate_positions,
    POSITION_GAP,
)


class TestCalculatePosition:

    def test_midpoint_between_two_positions(self):
        result = calculate_position(1000, 3000)
        assert result == 2000

    def test_when_before_is_none_places_after(self):
        """When before_pos is None, item goes at end (after + 2*GAP midpoint)."""
        result = calculate_position(1000, None)
        # before defaults to 1000 + 2*1000 = 3000
        # midpoint = (1000 + 3000) // 2 = 2000
        assert result == 2000

    def test_adjacent_positions(self):
        result = calculate_position(1000, 2000)
        assert result == 1500

    def test_very_close_positions(self):
        result = calculate_position(1000, 1002)
        assert result == 1001

    def test_same_position_returns_same(self):
        result = calculate_position(1000, 1000)
        assert result == 1000  # collision case

    def test_zero_and_gap(self):
        result = calculate_position(0, POSITION_GAP)
        assert result == POSITION_GAP // 2


class TestNeedsRenumber:

    def test_collision_with_after(self):
        assert needs_renumber(1000, 1002, 1000) is True

    def test_collision_with_before(self):
        assert needs_renumber(1000, 1002, 1002) is True

    def test_no_collision(self):
        assert needs_renumber(1000, 3000, 2000) is False

    def test_adjacent_collision(self):
        """When after and before are 1 apart, midpoint equals one of them."""
        new_pos = calculate_position(1000, 1001)
        assert needs_renumber(1000, 1001, new_pos) is True


class TestGeneratePositions:

    def test_generates_correct_count(self):
        positions = generate_positions(5)
        assert len(positions) == 5

    def test_positions_are_evenly_spaced(self):
        positions = generate_positions(4)
        assert positions == [1000, 2000, 3000, 4000]

    def test_positions_start_at_gap(self):
        positions = generate_positions(1)
        assert positions == [POSITION_GAP]

    def test_zero_count(self):
        assert generate_positions(0) == []

    def test_positions_are_increasing(self):
        positions = generate_positions(10)
        for i in range(1, len(positions)):
            assert positions[i] > positions[i - 1]
