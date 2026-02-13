""" Unit tests for core.navigation.pure_nav module """

import json
import math
import pytest

from core.navigation import pure_nav
from core.exceptions import InvalidStateError
# =============================================================================
# Fixtures: Sample state objects
# =============================================================================


@pytest.fixture
def basic_state():
    """State facing North with links in all 8 directions"""
    return {
        "panoId": "test_pano_123",
        "pov": {"heading": 0.0, "pitch": 0.0, "zoom": 1.0},
        "links": [
            {"heading": 0.0, "panoId": "north_pano"},      # N
            {"heading": 45.0, "panoId": "northeast_pano"}, # NE
            {"heading": 90.0, "panoId": "east_pano"},      # E
            {"heading": 135.0, "panoId": "southeast_pano"},# SE
            {"heading": 180.0, "panoId": "south_pano"},    # S
            {"heading": 225.0, "panoId": "southwest_pano"},# SW
            {"heading": 270.0, "panoId": "west_pano"},     # W
            {"heading": 315.0, "panoId": "northwest_pano"},# NW
        ],
    }

@pytest.fixture
def no_links_state():
    """State with no available links."""
    return {
        "panoId": "dead_end_pano",
        "pov": {"heading": 90.0, "pitch": 10.0, "zoom": 2.0},
        "links": [],
    }

@pytest.fixture
def partial_links_state():
    """State with only North and South links"""
    return {
        "panoId": "corridor_pano",
        "pov": {"heading": 45.0, "pitch": 0.0, "zoom": 1.0},
        "links": [
            {"heading": 5.0, "panoId": "north_pano"},
            {"heading": 185.0, "panoId": "south_pano"},
        ],
    }

# =============================================================================
# Tests: _normalize_heading (internal but critical)
# =============================================================================

class TestNormalizeHeading:
    """Tests for heading normalization"""

    def test_zero(self):
        assert pure_nav._normalize_heading(0.0) == 0.0

    def test_positive_within_range(self):
        assert pure_nav._normalize_heading(90.0) == 90.0
        assert pure_nav._normalize_heading(180.0) == 180.0
        assert pure_nav._normalize_heading(359.9) == 359.9

    def test_exactly_360_wraps_to_zero(self):
        assert pure_nav._normalize_heading(360.0) == 0.0

    def test_greater_than_360(self):
        assert pure_nav._normalize_heading(450.0) == 90.0
        assert pure_nav._normalize_heading(720.0) == 0.0

    def test_negative_values(self):
        assert pure_nav._normalize_heading(-90.0) == 270.0
        assert pure_nav._normalize_heading(-180.0) == 180.0
        assert pure_nav._normalize_heading(-360.0) == 0.0
        assert pure_nav._normalize_heading(-450.0) == 270.0

    def test_none_returns_zero(self):
        assert pure_nav._normalize_heading(None) == 0.0

    def test_non_numeric_returns_zero(self):
        assert pure_nav._normalize_heading("invalid") == 0.0
        assert pure_nav._normalize_heading([]) == 0.0

    def test_infinity_returns_zero(self):
        assert pure_nav._normalize_heading(float("inf")) == 0.0
        assert pure_nav._normalize_heading(float("-inf")) == 0.0

    def test_nan_returns_zero(self):
        assert pure_nav._normalize_heading(float("nan")) == 0.0

class TestHeadingToDirection:
    """Tests for converting heading to compass direction."""

    @pytest.mark.parametrize("heading, expected", [
        (0.0, "N"),
        (22.4, "N"),
        (337.5, "N"),
        (359.9, "N"),
    ])

    def test_north(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading, expected",[
        (22.5, "NE"),
        (45.0, "NE"),
        (67.4, "NE")
    ])
    def test_northeast(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading,expected", [
        (67.5, "E"),
        (90.0, "E"),
        (112.4, "E"),
    ])
    def test_east(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading,expected", [
        (112.5, "SE"),
        (135.0, "SE"),
        (157.4, "SE"),
    ])
    def test_southeast(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading,expected", [
        (157.5, "S"),
        (180.0, "S"),
        (202.4, "S"),
    ])
    def test_south(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading,expected", [
        (202.5, "SW"),
        (225.0, "SW"),
        (247.4, "SW"),
    ])
    def test_southwest(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading,expected", [
        (247.5, "W"),
        (270.0, "W"),
        (292.4, "W"),
    ])
    def test_west(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

    @pytest.mark.parametrize("heading,expected", [
        (292.5, "NW"),
        (315.0, "NW"),
        (337.4, "NW"),
    ])
    def test_northwest(self, heading, expected):
        assert pure_nav._heading_to_direction(heading) == expected

# =============================================================================
# Tests: check_direction
# =============================================================================
class TestCheckDirection:
    """Tests for check_direction function."""

    def test_facing_north(self, basic_state):
        basic_state["pov"]["heading"] = 0.0
        result = json.loads(pure_nav.check_direction(json.dumps(basic_state)))

        assert result["type"] == "result"
        assert result["updates"]["direction"] == "N"
        assert result["updates"]["heading"] == 0.0

    def test_facing_east(self, basic_state):
        basic_state["pov"]["heading"] = 90.0
        result = json.loads(pure_nav.check_direction(json.dumps(basic_state)))
        
        assert result["updates"]["direction"] == "E"
        assert result["updates"]["heading"] == 90.0

    def test_description_format(self, basic_state):
        basic_state["pov"]["heading"] = 45.0
        result = json.loads(pure_nav.check_direction(json.dumps(basic_state)))
        
        assert "Facing NE" in result["updates"]["description"]
        assert "45.0" in result["updates"]["description"]

# =============================================================================
# Tests: check_available_moves
# =============================================================================
class TestCheckAvailableMoves:
    """Tests for check_available_moves function."""
    
    def test_all_direcitons_available(self, basic_state):
        result = json.loads(pure_nav.check_available_moves(json.dumps(basic_state)))

        assert result["type"] == "result"
        moves = result["updates"]["available_moves"]

        # Should include universal actions
        assert "scroll_up" in moves
        assert "scroll_down" in moves
        assert "scroll_left" in moves
        assert "scroll_right" in moves
        assert "zoom_in" in moves
        assert "zoom_out" in moves

        # Should include all 8 movement directions (format is move_north, move_east, etc.)
        assert "move_north" in moves

    def test_no_links_only_universal_actions(self, no_links_state):
        result = json.loads(pure_nav.check_available_moves(json.dumps(no_links_state)))

        moves = result["updates"]["available_moves"]

        # Should have exactly 6 universal actions
        universal = ["scroll_up", "scroll_down", "scroll_left", "scroll_right", "zoom_in", "zoom_out"]
        for action in universal:  
            assert action in moves

    def test_partial_links(self, partial_links_state):
        result = json.loads(pure_nav.check_available_moves(json.dumps(partial_links_state)))

        moves = result["updates"]["available_moves"]
        move_directions = [m for m in moves if m.startswith("move_")]

        # Should have north and south moves
        assert len(move_directions) == 2

# =============================================================================
# Tests: Movement functions
# =============================================================================

class TestMovementFunctions:
    """Tests for move_* functions."""

    def test_move_north_succes(self, basic_state):
        result = json.loads(pure_nav.move_north(json.dumps(basic_state)))

        assert result["type"] == "result"
        assert result["updates"]["next_pano_id"] == "north_pano"

    def test_move_east_succes(self, basic_state):
        result = json.loads(pure_nav.move_east(json.dumps(basic_state)))

        assert result["type"] == "result" 
        assert result["updates"]["next_pano_id"] == "east_pano"           

    def test_move_south_success(self, basic_state):
        result = json.loads(pure_nav.move_south(json.dumps(basic_state)))
        
        assert result["updates"]["next_pano_id"] == "south_pano"

    def test_move_west_success(self, basic_state):
        result = json.loads(pure_nav.move_west(json.dumps(basic_state)))
        
        assert result["updates"]["next_pano_id"] == "west_pano"

    def test_move_northeast_success(self, basic_state):
        result = json.loads(pure_nav.move_northeast(json.dumps(basic_state)))
        
        assert result["updates"]["next_pano_id"] == "northeast_pano"

    def test_move_southeast_success(self, basic_state):
        result = json.loads(pure_nav.move_southeast(json.dumps(basic_state)))
        
        assert result["updates"]["next_pano_id"] == "southeast_pano"

    def test_move_southwest_success(self, basic_state):
        result = json.loads(pure_nav.move_southwest(json.dumps(basic_state)))
        
        assert result["updates"]["next_pano_id"] == "southwest_pano"

    def test_move_northwest_success(self, basic_state):
        result = json.loads(pure_nav.move_northwest(json.dumps(basic_state)))
        
        assert result["updates"]["next_pano_id"] == "northwest_pano"

    def test_move_no_link_in_direction(self, no_links_state):
        result = json.loads(pure_nav.move_north(json.dumps(no_links_state)))
        
        assert result["type"] == "result"
        assert result["updates"]["ok"] is False
        assert "error" in result["updates"]

    def test_move_partial_links_available_direction(self, partial_links_state):
        result = json.loads(pure_nav.move_north(json.dumps(partial_links_state)))
        
        assert result["type"] == "result"
        assert result["updates"]["next_pano_id"] == "north_pano"

    def test_move_partial_links_unavailable_direction(self, partial_links_state):
        result = json.loads(pure_nav.move_east(json.dumps(partial_links_state)))
        
        assert result["type"] == "result"
        assert result["updates"]["ok"] is False

# =============================================================================
# Tests: Scroll functions
# =============================================================================
class TestScrollFunctions:
    """Tests for scroll_* functions."""

    def test_scroll_left(self, basic_state):
        basic_state["pov"]["heading"] = 90.0
        result = json.loads(pure_nav.scroll_left(json.dumps(basic_state), 30.0))

        assert result["type"] == "result"
        assert result["updates"]["new_heading"] == 60.0

    def test_scroll_right(self, basic_state):
        basic_state["pov"]["heading"] = 90.0
        result = json.loads(pure_nav.scroll_right(json.dumps(basic_state), 30.0))
        
        assert result["updates"]["new_heading"] == 120.0

    def test_scroll_left_wrap_around(self, basic_state):
        basic_state["pov"]["heading"] = 10.0
        result = json.loads(pure_nav.scroll_left(json.dumps(basic_state), 30.0))
        
        # 10 - 30 = -20 -> normalized to 340
        assert result["updates"]["new_heading"] == 340.0

    def test_scroll_right_wrap_around(self, basic_state):
        basic_state["pov"]["heading"] = 350.0
        result = json.loads(pure_nav.scroll_right(json.dumps(basic_state), 30.0))
        
        # 350 + 30 = 380 -> normalized to 20
        assert result["updates"]["new_heading"] == 20.0

    def test_scroll_up(self, basic_state):
        basic_state["pov"]["pitch"] = 0.0
        result = json.loads(pure_nav.scroll_up(json.dumps(basic_state), 30.0))
        
        assert result["updates"]["new_pitch"] == 30.0

    def test_scroll_down(self, basic_state):
        basic_state["pov"]["pitch"] = 0.0
        result = json.loads(pure_nav.scroll_down(json.dumps(basic_state), 30.0))
        
        assert result["updates"]["new_pitch"] == -30.0

    def test_scroll_up_clamped_at_90(self, basic_state):
        basic_state["pov"]["pitch"] = 80.0
        result = json.loads(pure_nav.scroll_up(json.dumps(basic_state), 30.0))
        
        # 80 + 30 = 110, clamped to 90
        assert result["updates"]["new_pitch"] == 90.0

    def test_scroll_down_clamped_at_negative_90(self, basic_state):
        basic_state["pov"]["pitch"] = -80.0
        result = json.loads(pure_nav.scroll_down(json.dumps(basic_state), 30.0))
        
        # -80 - 30 = -110, clamped to -90
        assert result["updates"]["new_pitch"] == -90.0

    def test_scroll_with_negative_delta_uses_absolute(self, basic_state):
        basic_state["pov"]["heading"] = 90.0
        result = json.loads(pure_nav.scroll_left(json.dumps(basic_state), -30.0))
        
        # Should use abs(-30) = 30
        assert result["updates"]["new_heading"] == 60.0

    def test_scroll_invalid_delta_none(self, basic_state):
        result = json.loads(pure_nav.scroll_left(json.dumps(basic_state), None))
        
        assert result["type"] == "result"
        assert result["updates"]["ok"] is False

    def test_scroll_invalid_delta_nan(self, basic_state):
        result = json.loads(pure_nav.scroll_left(json.dumps(basic_state), float("nan")))
        
        assert result["type"] == "result"
        assert result["updates"]["ok"] is False

# =============================================================================
# Tests: Zoom functions
# =============================================================================

class TestZoomFunctions:
    """Tests for zoom_in and zoom_out functions."""

    def test_zoom_in(self, basic_state):
        basic_state["pov"]["zoom"] = 1.0
        result = json.loads(pure_nav.zoom_in(json.dumps(basic_state), 1.0))
        
        assert result["type"] == "result"
        assert result["updates"]["new_zoom"] == 2.0

    def test_zoom_out(self, basic_state):
        basic_state["pov"]["zoom"] = 2.0
        result = json.loads(pure_nav.zoom_out(json.dumps(basic_state), 1.0))
        
        assert result["updates"]["new_zoom"] == 1.0

    def test_zoom_out_clamped_at_zero(self, basic_state):
        basic_state["pov"]["zoom"] = 0.5
        result = json.loads(pure_nav.zoom_out(json.dumps(basic_state), 1.0))
        
        # 0.5 - 1.0 = -0.5, clamped to 0
        assert result["updates"]["new_zoom"] == 0.0

    def test_zoom_with_negative_delta_uses_absolute(self, basic_state):
        basic_state["pov"]["zoom"] = 1.0
        result = json.loads(pure_nav.zoom_in(json.dumps(basic_state), -1.0))
        
        # Should use abs(-1) = 1
        assert result["updates"]["new_zoom"] == 2.0

    def test_zoom_invalid_delta(self, basic_state):
        result = json.loads(pure_nav.zoom_in(json.dumps(basic_state), None))
        
        assert result["type"] == "result"
        assert result["updates"]["ok"] is False

# =============================================================================
# Tests: Edge cases and error handling
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_invalid_json_raises(self):
        with pytest.raises(InvalidStateError, match="invalid_state_json"):
            pure_nav.check_direction("not valid json")


    def test_missing_pov_raise(self):
        state = {"panoId": "test", "links": []}
        with pytest.raises(InvalidStateError, match="missing_state_keys"):
            pure_nav.check_direction(json.dumps(state))

    def test_missing_links_raises(self):
        state = {"panoId": "test", "pov": {"heading": 0, "pitch": 0, "zoom": 1}}
        with pytest.raises(InvalidStateError, match="missing_state_keys"):
            pure_nav.check_direction(json.dumps(state))

    def test_boundary_heading_22_5(self, basic_state):
        """Test exact boundary at 22.5 degrees (NE starts here)."""
        basic_state["pov"]["heading"] = 22.5
        result = json.loads(pure_nav.check_direction(json.dumps(basic_state)))
        assert result["updates"]["direction"] == "NE"

    def test_boundary_heading_337_5(self, basic_state):
        """Test exact boundary at 337.5 degrees (N starts here)."""
        basic_state["pov"]["heading"] = 337.5
        result = json.loads(pure_nav.check_direction(json.dumps(basic_state)))
        assert result["updates"]["direction"] == "N"
