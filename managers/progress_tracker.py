"""Live job progress from ComfyUI's WebSocket, for status/ETA reporting.

ComfyUI only reports sampling progress (step N of M) over its /ws socket;
the HTTP API just says whether a prompt is queued, running or done. Jobs
submitted with this tracker's client_id have their events sent to its
socket; jobs submitted by other clients (e.g. the ComfyUI UI) show only
elapsed time.

ETAs:
- stage ETA: remaining steps of the node currently sampling x its measured
  seconds/step (available after two progress events)
- overall ETA: from how long the same workflow took on earlier runs in this
  server session, minus elapsed time (available after one completed run)
"""
import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, Optional

logger = logging.getLogger("MCP_Server")

RECONNECT_SECONDS = 5
KEEP_FINISHED_SECONDS = 3600
DURATION_HISTORY = 5


class _Job:
    def __init__(self, now: float):
        self.started = now
        self.finished: Optional[float] = None
        self.status = "running"
        self.node: Optional[str] = None
        self.nodes_done: set = set()
        self.nodes_cached: set = set()
        self.step: Optional[int] = None
        self.steps: Optional[int] = None
        # (time, step) for the current node, to measure seconds/step
        self.step_times: deque = deque(maxlen=20)
        self.workflow_id: Optional[str] = None


class ProgressTracker:
    def __init__(self, comfyui_url: str, clock=time.time):
        base = comfyui_url.rstrip("/")
        base = "wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]
        # Submit prompts with this client_id (ComfyUIClient.client_id) -
        # ComfyUI sends step progress only to the submitting client.
        self.client_id = uuid.uuid4().hex
        self.ws_url = f"{base}/ws?clientId={self.client_id}"
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: Dict[str, _Job] = {}
        self._durations: Dict[str, deque] = {}
        self._thread: Optional[threading.Thread] = None
        self.connected = False

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="comfyui-progress", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        import websocket  # websocket-client

        while True:
            try:
                ws = websocket.create_connection(self.ws_url, timeout=10)
                ws.settimeout(None)
                self.connected = True
                logger.info("Progress tracker connected to ComfyUI")
                while True:
                    message = ws.recv()
                    if isinstance(message, str):  # binary frames are image previews
                        try:
                            self.handle(json.loads(message))
                        except ValueError:
                            pass
            except Exception as e:
                # Normal while ComfyUI is idle/stopped - it starts on demand.
                if self.connected:
                    logger.info("Progress tracker disconnected from ComfyUI: %s", e)
                self.connected = False
                time.sleep(RECONNECT_SECONDS)

    def handle(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        data = message.get("data") or {}
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return
        now = self._clock()
        with self._lock:
            job = self._jobs.get(prompt_id)
            if kind == "execution_start" or job is None:
                if kind in ("execution_success", "execution_error", "execution_interrupted"):
                    return
                job = self._jobs.setdefault(prompt_id, _Job(now))
            if kind == "execution_cached":
                job.nodes_cached.update(str(n) for n in data.get("nodes") or [])
            elif kind == "executing":
                node = data.get("node")
                if job.node is not None and job.node != node:
                    job.nodes_done.add(job.node)
                if node is None:
                    self._finish(prompt_id, job, "completed", now)
                elif node != job.node:
                    job.node = str(node)
                    job.step = job.steps = None
                    job.step_times.clear()
            elif kind == "progress":
                node = data.get("node")
                if node is not None and str(node) != job.node:
                    if job.node is not None:
                        job.nodes_done.add(job.node)
                    job.node = str(node)
                    job.step_times.clear()
                job.step, job.steps = data.get("value"), data.get("max")
                job.step_times.append((now, job.step))
            elif kind == "execution_success":
                self._finish(prompt_id, job, "completed", now)
            elif kind == "execution_error":
                self._finish(prompt_id, job, "error", now)
            elif kind == "execution_interrupted":
                self._finish(prompt_id, job, "interrupted", now)
            self._prune(now)

    def _finish(self, prompt_id: str, job: _Job, status: str, now: float) -> None:
        if job.finished is not None:
            return
        if job.node is not None:
            job.nodes_done.add(job.node)
        job.finished, job.status, job.node = now, status, None
        if status == "completed" and job.workflow_id:
            self._record_duration(job.workflow_id, now - job.started)

    def _record_duration(self, workflow_id: str, seconds: float) -> None:
        self._durations.setdefault(workflow_id, deque(maxlen=DURATION_HISTORY)).append(seconds)

    def _prune(self, now: float) -> None:
        stale = [pid for pid, j in self._jobs.items() if j.finished and now - j.finished > KEEP_FINISHED_SECONDS]
        for pid in stale:
            del self._jobs[pid]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def label(self, prompt_id: str, workflow_id: str) -> None:
        """Tie a job to its workflow so its duration feeds future ETAs.
        Safe to call after the job already finished."""
        with self._lock:
            job = self._jobs.get(prompt_id)
            if job is None:
                job = self._jobs[prompt_id] = _Job(self._clock())
            if job.workflow_id is None:
                job.workflow_id = workflow_id
                if job.finished is not None and job.status == "completed":
                    self._record_duration(workflow_id, job.finished - job.started)

    def snapshot(self, prompt_id: str, prompt: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """Progress for a running job. `prompt` (the API workflow from the
        queue entry) adds node names and totals when available."""
        now = self._clock()
        with self._lock:
            job = self._jobs.get(prompt_id)
            if job is None or job.finished is not None:
                return None
            out: Dict[str, Any] = {"elapsed_seconds": round(now - job.started)}
            if job.node is not None:
                out["current_node"] = job.node
                if prompt and job.node in prompt:
                    out["stage"] = prompt[job.node].get("class_type")
            if job.steps:
                out["step"], out["steps"] = job.step, job.steps
                out["stage_percent"] = round(100 * (job.step or 0) / job.steps)
                rate = self._seconds_per_step(job)
                if rate is not None:
                    out["seconds_per_step"] = round(rate, 1)
                    out["stage_eta_seconds"] = round(rate * (job.steps - (job.step or 0)))
            if prompt:
                done = len(job.nodes_done | job.nodes_cached)
                out["nodes_done"], out["nodes_total"] = min(done, len(prompt)), len(prompt)
            history = self._durations.get(job.workflow_id or "")
            if history:
                typical = sum(history) / len(history)
                out["typical_duration_seconds"] = round(typical)
                out["eta_seconds"] = max(0, round(typical - (now - job.started)))
            elif "stage_eta_seconds" in out:
                out["note"] = "ETA covers the current sampling stage; later stages (e.g. decode, upscale) add some time."
            if not self.connected:
                out["warning"] = "Not connected to ComfyUI's progress feed; progress may be stale."
            return out

    @staticmethod
    def _seconds_per_step(job: _Job) -> Optional[float]:
        if len(job.step_times) < 2:
            return None
        (t0, s0), (t1, s1) = job.step_times[0], job.step_times[-1]
        if s1 is None or s0 is None or s1 <= s0:
            return None
        return (t1 - t0) / (s1 - s0)
