"""Tests for ProgressTracker (managers/progress_tracker.py)"""
from managers.progress_tracker import ProgressTracker

PROMPT = {
    "1": {"class_type": "UNETLoader", "inputs": {}},
    "2": {"class_type": "KSampler", "inputs": {}},
    "3": {"class_type": "SaveImage", "inputs": {}},
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _event(kind, **data):
    return {"type": kind, "data": {"prompt_id": "p1", **data}}


def _run_until_sampling(tracker, clock):
    tracker.handle(_event("execution_start"))
    tracker.handle(_event("executing", node="1"))
    clock.now += 10
    tracker.handle(_event("executing", node="2"))
    for step in (1, 2, 3):
        clock.now += 5
        tracker.handle(_event("progress", node="2", value=step, max=20))


def test_stage_progress_and_eta():
    clock = Clock()
    tracker = ProgressTracker("http://localhost:8188", clock=clock)
    tracker.connected = True
    _run_until_sampling(tracker, clock)

    snap = tracker.snapshot("p1", PROMPT)
    assert snap["stage"] == "KSampler"
    assert (snap["step"], snap["steps"]) == (3, 20)
    assert snap["stage_percent"] == 15
    assert snap["seconds_per_step"] == 5.0
    assert snap["stage_eta_seconds"] == 85
    assert snap["elapsed_seconds"] == 25
    assert (snap["nodes_done"], snap["nodes_total"]) == (1, 3)
    assert "eta_seconds" not in snap  # no earlier run to go by


def test_overall_eta_from_previous_run():
    clock = Clock()
    tracker = ProgressTracker("http://localhost:8188", clock=clock)
    tracker.connected = True
    _run_until_sampling(tracker, clock)
    tracker.label("p1", "book_cover")
    clock.now += 75
    tracker.handle(_event("execution_success"))
    assert tracker.snapshot("p1") is None  # finished

    # Second run of the same workflow: ETA from the first run's 100s.
    second = {"type": "execution_start", "data": {"prompt_id": "p2"}}
    tracker.handle(second)
    tracker.label("p2", "book_cover")
    clock.now += 40
    snap = tracker.snapshot("p2")
    assert snap["typical_duration_seconds"] == 100
    assert snap["eta_seconds"] == 60


def test_label_after_completion_still_records_duration():
    clock = Clock()
    tracker = ProgressTracker("http://localhost:8188", clock=clock)
    tracker.handle(_event("execution_start"))
    clock.now += 30
    tracker.handle(_event("executing", node=None))  # ComfyUI's "done" signal
    tracker.label("p1", "quick")  # run_workflow labels after the job returns
    assert list(tracker._durations["quick"]) == [30]


def test_ws_url_from_http_url():
    assert ProgressTracker("http://127.0.0.1:8188").ws_url.startswith("ws://127.0.0.1:8188/ws?clientId=")
    assert ProgressTracker("https://comfy.example/").ws_url.startswith("wss://comfy.example/ws?clientId=")
