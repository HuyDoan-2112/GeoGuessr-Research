
"""Verify JobRunner respects concurrency limits."""

import time
import threading
from apps.job_runner import JobRunner, Job

def test_bounded_concurrency():
    """Verify max 30 jobs run at once."""
    max_workers = 5  # Use 5 for quick test
    total_jobs = 20
    
    running_count = [0]
    max_running = [0]
    lock = threading.Lock()
    
    def slow_job():
        with lock:
            running_count[0] += 1
            max_running[0] = max(max_running[0], running_count[0])
        
        time.sleep(0.5)  # Simulate work
        
        with lock:
            running_count[0] -= 1
        
        return "done"
    
    runner = JobRunner(max_workers=max_workers)
    runner.start()
    
    for i in range(total_jobs):
        runner.submit(Job(id=f"job_{i}", execute_fn=slow_job))
    
    runner.wait_all()
    runner.stop()
    
    stats = runner.get_stats()
    
    assert max_running[0] <= max_workers, f"Exceeded max workers: {max_running[0]} > {max_workers}"
    assert stats["completed"] == total_jobs, f"Not all jobs completed: {stats['completed']}/{total_jobs}"
    
    print(f"✓ Bounded concurrency works (max concurrent: {max_running[0]}, limit: {max_workers})")

def test_job_retry():
    """Verify jobs retry on failure."""
    attempt_counts = {}
    
    def flaky_job(job_id):
        attempt_counts[job_id] = attempt_counts.get(job_id, 0) + 1
        if attempt_counts[job_id] < 2:
            raise RuntimeError("Flaky failure")
        return "success"
    
    runner = JobRunner(max_workers=2)
    runner.start()
    
    for i in range(3):
        job_id = f"flaky_{i}"
        runner.submit(Job(
            id=job_id,
            execute_fn=lambda jid=job_id: flaky_job(jid),
            max_attempts=3,
        ))
    
    runner.wait_all()
    runner.stop()
    
    stats = runner.get_stats()
    assert stats["completed"] == 3, f"Expected 3 completed, got {stats['completed']}"
    assert stats["failed"] == 0, f"Expected 0 failed, got {stats['failed']}"
    
    print("✓ Job retry works")

if __name__ == "__main__":
    test_bounded_concurrency()
    test_job_retry()
