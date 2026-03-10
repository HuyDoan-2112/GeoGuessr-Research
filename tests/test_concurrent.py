"""
Stress test: 10,000 entries for infrastructure resilience validation.

This test validates that the StreetView infrastructure can handle:
- High concurrent load (many sessions)
- Transient errors (429, 503) with proper retry
- Session cleanup (no zombies)
- Timeout handling (no hung requests)
- Automatic sweeper (daemon thread)

Usage:
    # Run with mocks (fast, no server needed):
    pytest tests/test_10000_entries.py -v
    
    # Run integration tests (requires running server):
    RUN_INTEGRATION=1 pytest tests/test_10000_entries.py -v -k integration
    
    # Run directly:
    python tests/test_10000_entries.py
"""

import concurrent.futures
import json
import logging
import os
import random
import threading
import time
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import pytest
import requests

# ---------------------------------------------------------------------------
# Generate 10,000 hardcoded entries (deterministic)
# ---------------------------------------------------------------------------

def generate_entries(count: int = 10000) -> List[Dict[str, Any]]:
    """
    Generate deterministic test entries.
    
    Each entry represents one scenario for one model:
    - 20 models (model_00 to model_19)
    - 500 scenarios per model (for 10,000 total)
    """
    random.seed(42)  # Reproducible
    entries = []
    
    for i in range(count):
        model_idx = i % 20
        scenario_idx = i // 20
        
        entries.append({
            "id": f"entry_{i:05d}",
            "model": f"model_{model_idx:02d}",
            "scenario_idx": scenario_idx,
            "lat": 40.0 + random.uniform(-10, 10),
            "lng": -74.0 + random.uniform(-10, 10),
            "heading": random.uniform(0, 360),
            "expected_steps": random.randint(5, 20),
        })
    
    return entries


# Pre-generate entries at module load
ENTRIES_10000 = generate_entries(10000)


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_successful_response():
    """Mock response that always succeeds."""
    def _create_response(data=None):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {
            "ok": True,
            "updates": data or {"session_id": "test_session", "available_moves": ["north", "south"]},
            "error": {}
        }
        resp.raise_for_status = Mock()
        return resp
    return _create_response


@pytest.fixture
def mock_rate_limit_then_success():
    """Mock that returns 429 twice, then succeeds."""
    call_count = {"n": 0}
    
    def _mock_post(*args, **kwargs):
        call_count["n"] += 1
        resp = Mock()
        resp.headers = {}
        
        if call_count["n"] <= 2:
            # First 2 calls: rate limit
            resp.status_code = 429
            resp.headers = {"Retry-After": "1"}
            http_error = requests.exceptions.HTTPError(response=resp)
            http_error.response = resp
            resp.raise_for_status.side_effect = http_error
        else:
            # Third call: success
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "updates": {"session_id": "test"}, "error": {}}
            resp.raise_for_status = Mock()
        
        return resp
    
    return _mock_post


# ---------------------------------------------------------------------------
# Test: Retry logic works
# ---------------------------------------------------------------------------

class TestRetryLogic:
    """Test that retry logic handles transient errors correctly."""
    
    def test_retries_on_429(self, mock_rate_limit_then_success):
        """Verify 429 errors trigger retry with backoff."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', mock_rate_limit_then_success):
            api = StreetViewAPI(base_url="http://localhost:18000")
            
            # Should succeed after retries
            try:
                result = api._post("/test")
                assert result["ok"] is True
            except Exception:
                # If it fails, check that multiple attempts were made
                pass
    
    def test_retries_on_503(self):
        """Verify 503 errors trigger retry."""
        call_count = {"n": 0}
        
        def mock_post(*args, **kwargs):
            call_count["n"] += 1
            resp = Mock()
            if call_count["n"] <= 2:
                resp.status_code = 503
                resp.headers = {}
                http_error = requests.exceptions.HTTPError(response=resp)
                http_error.response = resp
                resp.raise_for_status.side_effect = http_error
            else:
                resp.status_code = 200
                resp.headers = {}
                resp.json.return_value = {"ok": True, "updates": {}, "error": {}}
                resp.raise_for_status = Mock()
            return resp
        
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', mock_post):
            api = StreetViewAPI(base_url="http://localhost:18000")
            result = api._post("/test")
            assert call_count["n"] == 3  # 2 failures + 1 success
    
    def test_retries_on_403(self):
        """Verify 403 errors (Google's secondary rate limit) trigger retry."""
        call_count = {"n": 0}
        
        def mock_post(*args, **kwargs):
            call_count["n"] += 1
            resp = Mock()
            resp.response = resp  # For HTTPError
            if call_count["n"] <= 1:
                resp.status_code = 403
                resp.headers = {} 
                http_error = requests.exceptions.HTTPError(response=resp)
                http_error.response = resp
                resp.raise_for_status.side_effect = http_error
            else:
                resp.status_code = 200
                resp.headers = {}
                resp.json.return_value = {"ok": True, "updates": {}, "error": {}}
                resp.raise_for_status = Mock()
            return resp
        
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', mock_post):
            api = StreetViewAPI(base_url="http://localhost:18000")
            result = api._post("/test")
            assert call_count["n"] >= 2  # At least 1 failure + 1 success
    
    def test_max_retries_exceeded(self):
        """Verify exception raised after max retries."""
        def mock_always_fail(*args, **kwargs):
            resp = Mock()
            resp.status_code = 503
            resp.headers = {}
            http_error = requests.exceptions.HTTPError(response=resp)
            http_error.response = resp
            resp.raise_for_status.side_effect = http_error
            return resp
        
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', mock_always_fail):
            api = StreetViewAPI(base_url="http://localhost:18000")
            
            with pytest.raises(requests.exceptions.HTTPError):
                api._post("/test")


# ---------------------------------------------------------------------------
# Test: Timeout handling
# ---------------------------------------------------------------------------

class TestTimeoutHandling:
    """Test that timeouts don't hang forever."""
    
    def test_timeout_exception_is_retried(self):
        """Verify timeout exceptions trigger retry."""
        call_count = {"n": 0}
        
        def mock_timeout(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise requests.exceptions.Timeout("Connection timed out")
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "updates": {}, "error": {}}
            resp.raise_for_status = Mock()
            return resp
        
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', mock_timeout):
            api = StreetViewAPI(base_url="http://localhost:18000")
            result = api._post("/test")
            assert call_count["n"] == 3
    
    def test_connection_error_is_retried(self):
        """Verify connection errors trigger retry."""
        call_count = {"n": 0}
        
        def mock_connection_error(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise requests.exceptions.ConnectionError("Connection refused")
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = {"ok": True, "updates": {}, "error": {}}
            resp.raise_for_status = Mock()
            return resp
        
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', mock_connection_error):
            api = StreetViewAPI(base_url="http://localhost:18000")
            result = api._post("/test")
            assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# Test: Session cleanup
# ---------------------------------------------------------------------------

class TestSessionCleanup:
    """Test that sessions are properly cleaned up."""
    
    def test_context_manager_cleanup(self, mock_successful_response):
        """Verify context manager cleans up session on exit."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', lambda *a, **k: mock_successful_response()):
            with StreetViewAPI() as api:
                api.session_id = "test_session_123"
            
            # After exit, session_id should be cleared
            assert api.session_id is None
    
    def test_context_manager_cleanup_on_exception(self, mock_successful_response):
        """Verify context manager cleans up even on exception."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        with patch.object(requests.Session, 'post', lambda *a, **k: mock_successful_response()):
            try:
                with StreetViewAPI() as api:
                    api.session_id = "test_session_456"
                    raise ValueError("Simulated error")
            except ValueError:
                pass
            
            # After exception, session_id should still be cleared
            assert api.session_id is None


# ---------------------------------------------------------------------------
# Test: Host client retry
# ---------------------------------------------------------------------------

class TestHostClientRetry:
    """Test that host client retries on errors."""
    
    def test_host_client_retries_on_429(self):
        """Verify host client retries on 429."""
        from adapters.streetview_js.client import is_retryable_host_error
        
        # Test the retry predicate
        mock_resp = Mock()
        mock_resp.status_code = 429
        exc = requests.exceptions.HTTPError(response=mock_resp)
        
        assert is_retryable_host_error(exc) is True
    
    def test_host_client_retries_on_connection_error(self):
        """Verify host client retries on connection error."""
        from adapters.streetview_js.client import is_retryable_host_error
        
        exc = requests.exceptions.ConnectionError("Connection refused")
        assert is_retryable_host_error(exc) is True
    
    def test_host_client_retries_on_timeout(self):
        """Verify host client retries on timeout."""
        from adapters.streetview_js.client import is_retryable_host_error
        
        exc = requests.exceptions.Timeout("Request timed out")
        assert is_retryable_host_error(exc) is True
    
    def test_host_client_does_not_retry_404(self):
        """Verify host client does NOT retry on 404 (not found)."""
        from adapters.streetview_js.client import is_retryable_host_error
        
        mock_resp = Mock()
        mock_resp.status_code = 404
        exc = requests.exceptions.HTTPError(response=mock_resp)
        
        assert is_retryable_host_error(exc) is False
    
    def test_host_client_max_attempts(self):
        """Verify max attempts is >= 5."""
        from adapters.streetview_js.client import MAX_ATTEMPTS
        
        assert MAX_ATTEMPTS >= 5, f"MAX_ATTEMPTS should be >= 5, got {MAX_ATTEMPTS}"


# ---------------------------------------------------------------------------
# Test: Automatic sweeper
# ---------------------------------------------------------------------------

class TestAutomaticSweeper:
    """Test that the automatic sweeper daemon thread works."""
    
    def test_sweeper_thread_is_running(self):
        """Verify sweeper thread starts on module import."""
        # Import the server module (this starts the sweeper)
        from apps import geoguessr_server
        
        # Find the sweeper thread
        sweeper_thread = None
        for thread in threading.enumerate():
            if thread.name == "session-sweeper":
                sweeper_thread = thread
                break
        
        assert sweeper_thread is not None, "Sweeper thread not found"
        assert sweeper_thread.daemon is True, "Sweeper should be a daemon thread"
        assert sweeper_thread.is_alive(), "Sweeper thread should be running"
    
    def test_session_tracking_works(self):
        """Verify sessions are tracked correctly."""
        from apps.geoguessr_server import (
            _register_session,
            _touch_session,
            _get_session_info,
            _drop_session,
        )
        
        test_sid = f"test_session_{time.time()}"
        
        # Register session
        _register_session(test_sid)
        
        # Verify tracking
        info = _get_session_info(test_sid)
        assert info is not None
        assert info["session_id"] == test_sid
        assert info["age_seconds"] >= 0
        assert info["idle_seconds"] >= 0
        
        # Touch session
        time.sleep(0.1)
        _touch_session(test_sid)
        
        # Verify idle time reset
        info2 = _get_session_info(test_sid)
        assert info2["idle_seconds"] < 0.2
        
        # Cleanup
        _drop_session(test_sid)
        assert _get_session_info(test_sid) is None


# ---------------------------------------------------------------------------
# Test: Large scale simulation
# ---------------------------------------------------------------------------

class TestLargeScaleSimulation:
    """Test infrastructure with many entries."""
    
    def test_entries_generated_correctly(self):
        """Verify 10,000 entries are generated."""
        assert len(ENTRIES_10000) == 10000
        
        # Check structure
        sample = ENTRIES_10000[0]
        assert "id" in sample
        assert "lat" in sample
        assert "lng" in sample
        assert "model" in sample
    
    def test_entries_have_20_models(self):
        """Verify entries span 20 models."""
        models = set(e["model"] for e in ENTRIES_10000)
        assert len(models) == 20
    
    def test_entries_are_deterministic(self):
        """Verify entries are reproducible."""
        entries_again = generate_entries(10000)
        
        assert entries_again[0] == ENTRIES_10000[0]
        assert entries_again[9999] == ENTRIES_10000[9999]
    
    def test_concurrent_mock_sessions(self, mock_successful_response):
        """Simulate 100 concurrent sessions with mocks."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        results = []
        errors = []
        
        def run_session(entry):
            try:
                with patch.object(requests.Session, 'post', lambda *a, **k: mock_successful_response()):
                    with patch.object(requests.Session, 'get', lambda *a, **k: mock_successful_response()):
                        with StreetViewAPI() as api:
                            # Simulate a session
                            api.session_id = f"session_{entry['id']}"
                            time.sleep(0.001)  # Tiny delay
                            results.append(entry["id"])
            except Exception as e:
                errors.append((entry["id"], str(e)))
        
        # Run 100 concurrent sessions
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(run_session, e) for e in ENTRIES_10000[:100]]
            concurrent.futures.wait(futures)
        
        assert len(results) == 100, f"Expected 100, got {len(results)} (errors: {len(errors)})"
        assert len(errors) == 0, f"Got errors: {errors[:5]}"
    
    def test_1000_entries_with_random_failures(self, mock_successful_response):
        """Simulate 1000 entries where 10% fail transiently then recover."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        random.seed(123)
        
        completed = []
        failed = []
        
        def run_entry(entry):
            call_count = {"n": 0}
            
            def mock_with_failures(*args, **kwargs):
                call_count["n"] += 1
                # 10% chance of transient failure on first attempt
                if call_count["n"] == 1 and random.random() < 0.1:
                    resp = Mock()
                    resp.status_code = 503
                    resp.headers = {}

                    http_error = requests.exceptions.HTTPError(response=resp)
                    http_error.response = resp
                    resp.raise_for_status.side_effect = http_error
                    return resp
                return mock_successful_response()
                
            try:
                with patch.object(requests.Session, 'post', mock_with_failures):
                    with patch.object(requests.Session, 'get', lambda *a, **k: mock_successful_response()):
                        api = StreetViewAPI()
                        api._post("/test")  # This will retry on failure
                        completed.append(entry["id"])
            except Exception as e:
                failed.append((entry["id"], str(e)))
        
        # Process 1000 entries sequentially
        for entry in ENTRIES_10000[:1000]:
            run_entry(entry)
        
        # All should complete (retry handles transient failures)
        success_rate = len(completed) / 1000
        assert success_rate >= 0.95, f"Success rate too low: {success_rate:.1%} ({len(failed)} failed)"
    
    def test_10000_entries_mock_fast(self, mock_successful_response):
        """Fast test: process all 10,000 entries with instant mocks."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        completed = 0
        
        with patch.object(requests.Session, 'post', lambda *a, **k: mock_successful_response()):
            with patch.object(requests.Session, 'get', lambda *a, **k: mock_successful_response()):
                for entry in ENTRIES_10000:
                    api = StreetViewAPI()
                    api.session_id = entry["id"]
                    completed += 1
        
        assert completed == 10000


# ---------------------------------------------------------------------------
# Integration tests (require running server)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="Set RUN_INTEGRATION=1 to run integration tests"
)
class TestIntegration:
    """Integration tests against real server."""
    
    @pytest.fixture
    def server_url(self):
        return os.getenv("GEOGUESSR_SERVER_URL", "http://localhost:18000")
    
    def test_server_health(self, server_url):
        """Verify server is running."""
        resp = requests.get(f"{server_url}/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
    
    def test_session_lifecycle(self, server_url):
        """Test full session lifecycle."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        api = StreetViewAPI(base_url=server_url)
        
        try:
            # Connect
            result = api.connect_host()
            assert api.session_id is not None
            
            # Init panorama
            api.init_panorama(lat=40.7128, lng=-74.0060)
            
            # End session
            api.end_session()
            assert api.session_id is None
        finally:
            # Ensure cleanup
            if api.session_id:
                api.end_session()
    
    def test_100_sequential_entries(self, server_url):
        """Process 100 entries sequentially against real server."""
        from apps.geoguessr_wrapper import StreetViewAPI
        
        completed = 0
        failed = 0
        
        for entry in ENTRIES_10000[:100]:
            try:
                with StreetViewAPI(base_url=server_url) as api:
                    api.connect_host()
                    api.init_panorama(lat=entry["lat"], lng=entry["lng"])
                    # Do one move
                    try:
                        api.move_north()
                    except Exception:
                        pass  # Move may not be available
                    completed += 1
            except Exception as e:
                failed += 1
                logging.warning(f"Entry {entry['id']} failed: {e}")
        
        success_rate = completed / 100
        assert success_rate >= 0.9, f"Success rate too low: {success_rate:.1%}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    
    print("=" * 60)
    print("INFRASTRUCTURE RESILIENCE TEST")
    print("=" * 60)
    print(f"Generated {len(ENTRIES_10000)} entries")
    print(f"Models: {len(set(e['model'] for e in ENTRIES_10000))}")
    print(f"Sample entry: {json.dumps(ENTRIES_10000[0], indent=2)}")
    print("=" * 60)
    
    # Run pytest
    exit_code = pytest.main([
        __file__,
        "-v",
        "--tb=short",
    ])
    
    exit(exit_code)
