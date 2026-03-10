import os
import time

import pytest
import requests

SERVER_URL = os.getenv("GEOGUESSR_SERVER_URL", "http://localhost:18000")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _require_server():
    try:
        resp = requests.get(f"{SERVER_URL}/health", timeout=2)
        if resp.status_code != 200:
            pytest.skip(f"GeoGuessr server not reachable on {SERVER_URL}")
    except requests.RequestException:
        pytest.skip(f"GeoGuessr server not reachable on {SERVER_URL}")

def test_no_zombie_leak(): 
    """Call _load_scenario multiple times - should not create multiple sessions."""

    from apps.geoguessr_wrapper import StreetViewAPI
    api = StreetViewAPI()
    
    # First load
    api._load_scenario({"lat": 40.7, "lng": -74.0})
    sid1 = api.session_id
    
    # Second load (simulating retry) - should reuse same session
    api._load_scenario({"lat": 40.8, "lng": -74.1})
    sid2 = api.session_id
    
    assert sid1 == sid2, f"Session changed! {sid1} -> {sid2} (LEAK!)"
    
    # Check server only has 1 session
    resp = requests.get(f"{SERVER_URL}/sessions")
    data = resp.json()
    assert data["updates"]["count"] == 1, f"Expected 1 session, got {data['updates']['count']}"
    
    # Cleanup
    api._end_session()
    print("✓ No leak on retry")

def test_cleanup_on_context_exit():
    """Verify session closed when using context manager."""
    from apps.geoguessr_wrapper import StreetViewAPI
    
    with StreetViewAPI() as api:
        api._load_scenario({"lat": 40.7, "lng": -74.0})
        sid = api.session_id
    
    # After context exit, session should be None
    assert api.session_id is None, "Session not cleared after context exit"
    
    # Server should have 0 sessions
    time.sleep(0.5)  # Brief wait for server to process
    resp = requests.get(f"{SERVER_URL}/sessions")
    data = resp.json()
    assert data["updates"]["count"] == 0, f"Expected 0 sessions, got {data['updates']['count']}"
    
    print("✓ Cleanup on context exit works")

def test_sweeper_cleans_orphans():
    """Verify sweeper cleans up abandoned sessions."""
    from unittest.mock import patch
    from apps.geoguessr_wrapper import StreetViewAPI
    import apps.geoguessr_server as server_mod

    if not os.getenv("RUN_SWEEPER_TEST"):
        pytest.skip("Set RUN_SWEEPER_TEST=1 to enable sweeper test (takes ~70s)")

    # Patch the module-level variables directly — setting os.environ has no
    # effect because they were already evaluated at import time.
    with patch.object(server_mod, "SESSION_IDLE_TIMEOUT", 5), \
         patch.object(server_mod, "SWEEP_INTERVAL", 5):
        api = StreetViewAPI()
        api._load_scenario({"lat": 40.7, "lng": -74.0})
        sid = api.session_id

        # DON'T call _end_session - simulate crash
        api.session_id = None  # Lose reference

        # Wait for sweeper (sweep interval + idle timeout + buffer)
        print("Waiting for sweeper...")
        time.sleep(15)  # 5s sweep interval + 5s idle + buffer

        # Session should be gone
        resp = requests.get(f"{SERVER_URL}/sessions")
        data = resp.json()
        sessions = data["updates"]["sessions"]

        orphan_exists = any(s["session_id"] == sid for s in sessions)
        assert not orphan_exists, f"Orphan session {sid} not swept!"

    print("✓ Sweeper cleaned orphan session")

if __name__ == "__main__":
    test_no_zombie_leak()
    test_cleanup_on_context_exit()
    # test_sweeper_cleans_orphans()  # Takes ~70s, run separately

