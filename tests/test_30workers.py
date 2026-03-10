"""Full integration test with 30 workers."""

import logging
import os
logging.basicConfig(level=logging.INFO)

import pytest
import requests

from apps.job_runner import JobRunner, create_streetview_job

SERVER_URL = os.getenv("GEOGUESSR_SERVER_URL", "http://localhost:18000")

pytestmark = pytest.mark.integration

def my_agent(api, scenario):
    """Your actual agent logic."""
    # Move around
    api.move_north()
    api.scroll_right(45)
    img = api.capture_view()
    return {"scenario_id": scenario["id"], "captured": True}

def test_full_integration():
    """Run multiple scenarios with proper cleanup."""

    # Preflight: server must be running
    try:
        resp = requests.get(f"{SERVER_URL}/health", timeout=2)
        if resp.status_code != 200:
            pytest.skip(f"GeoGuessr server not reachable on {SERVER_URL}")
    except requests.RequestException:
        pytest.skip(f"GeoGuessr server not reachable on {SERVER_URL}")
    
    scenarios = [
        {"id": f"test_{i}", "lat": 40.7 + i*0.01, "lng": -74.0}
        for i in range(10)  # Start with 10, scale up
    ]
    
    runner = JobRunner(max_workers=5)  # Start with 5
    runner.start()
    
    for scenario in scenarios:
        job = create_streetview_job(
            job_id=scenario["id"],
            scenario=scenario,
            agent_fn=my_agent,
        )
        runner.submit(job)
    
    runner.wait_all()
    
    stats = runner.get_stats()
    print(f"Completed: {stats['completed']}, Failed: {stats['failed']}")
    
    # Check no zombie sessions
    resp = requests.get(f"{SERVER_URL}/sessions")
    data = resp.json()
    session_count = data["updates"]["count"]
    print(f"Remaining sessions: {session_count}")
    
    assert session_count == 0, f"ZOMBIE LEAK: {session_count} sessions remaining!"
    
    runner.stop()
    print("✓ Full integration test passed")

if __name__ == "__main__":
    test_full_integration()
