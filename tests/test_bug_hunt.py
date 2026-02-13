import importlib
import logging
import types

import pytest


class TestSmokeImports:
    def test_import_exceptions(self):
        from core import exceptions  # noqa: F401
        assert hasattr(exceptions, "GeoGuessrError")

    def test_import_protocol(self):
        from core.navigation import protocol  # noqa: F401
        assert callable(protocol.parse_state)

    def test_import_pure_nav(self):
        from core.navigation import pure_nav  # noqa: F401
        assert callable(pure_nav.check_direction)

    def test_import_nav_tools(self):
        from core.tools import nav_tools  # noqa: F401
        assert callable(nav_tools.init_panorama)

    def test_import_image_utils(self):
        from core.utils import image_utils  # noqa: F401
        assert callable(image_utils.zoom_to_fov)

    def test_import_image_pipeline(self):
        from core.utils import image_pipeline  # noqa: F401
        assert callable(image_pipeline.capture_state_image)

    def test_import_client(self):
        from adapters.streetview_js.client import StreetViewHostClient  # noqa: F401
        assert StreetViewHostClient is not None

    def test_import_wrapper(self):
        from apps.geoguessr_wrapper import StreetViewAPI  # noqa: F401
        assert StreetViewAPI is not None

    def test_import_server(self):
        from apps.geoguessr_server import Engine  # noqa: F401
        assert Engine is not None


class TestLoggingConfig:
    def test_setup_logging_callable(self):
        mod = importlib.import_module("core.logging_config")
        assert callable(mod.setup_logging)
        mod.setup_logging(level=logging.INFO)


class TestInvariant_ServerEngineToolMapsComplete:
    """The Engine tool maps must cover the same functions that nav_tools exposes."""

    def test_move_tools_map_complete(self):
        from apps.geoguessr_server import Engine
        from core.tools import nav_tools

        expected = {
            "north", "northeast", "east", "southeast",
            "south", "southwest", "west", "northwest",
        }
        assert set(Engine.MOVE_TOOLS.keys()) == expected

        for direction, fn in Engine.MOVE_TOOLS.items():
            expected_fn = getattr(nav_tools, f"move_{direction}")
            assert fn is expected_fn

    def test_scroll_tools_map_complete(self):
        from apps.geoguessr_server import Engine
        from core.tools import nav_tools

        for direction in ("left", "right", "up", "down"):
            assert direction in Engine.SCROLL_TOOLS
            assert Engine.SCROLL_TOOLS[direction] is getattr(nav_tools, f"scroll_{direction}")

    def test_zoom_tools_map_complete(self):
        from apps.geoguessr_server import Engine
        from core.tools import nav_tools

        for direction in ("in", "out"):
            assert direction in Engine.ZOOM_TOOLS
            assert Engine.ZOOM_TOOLS[direction] is getattr(nav_tools, f"zoom_{direction}")
