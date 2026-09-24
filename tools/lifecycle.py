"""Lifecycle tools: start/stop/status for the on-demand ComfyUI process.

ComfyUI itself doesn't manage its own process lifecycle - it just runs until
killed. These tools delegate to a separate control API (comfyui_control.py,
running as its own systemd service) that actually starts/stops the ComfyUI
process and enforces an idle-timeout safety net.
"""

import os

import requests
from mcp.server.fastmcp import FastMCP

CONTROL_URL = os.getenv("COMFYUI_CONTROL_URL", "http://127.0.0.1:8189")
CONTROL_TOKEN = os.getenv("COMFYUI_CONTROL_TOKEN", "")


def _call(path: str, method: str = "GET") -> dict:
    headers = {"X-Control-Token": CONTROL_TOKEN}
    # /start can take up to ~60s while ComfyUI boots and loads its checkpoint.
    resp = requests.request(method, f"{CONTROL_URL}{path}", headers=headers, timeout=90)
    resp.raise_for_status()
    return resp.json()


def comfyui_running() -> bool:
    """Whether ComfyUI is up, per the control API. Raises
    requests.RequestException if the control API can't be reached."""
    return bool(_call("/status", "GET").get("running"))


def start_comfyui_and_refresh(comfyui_client=None, defaults_manager=None) -> dict:
    """Start ComfyUI via the control API (idempotent) and refresh model lists.

    comfyui_client and defaults_manager are constructed once at MCP-server
    boot, before ComfyUI itself is running, so their cached model lists are
    otherwise permanently empty (ComfyUI starts on demand, after the MCP
    server). defaults_manager keeps its own separate cached copy
    (_available_models_set), so both need refreshing independently.
    """
    result = _call("/start", "POST")
    if comfyui_client is not None:
        comfyui_client.refresh_models()
    if defaults_manager is not None:
        defaults_manager.refresh_model_set()
        # refresh_model_set() only updates the available-models cache; it
        # never clears _invalid_models (set once at boot, before ComfyUI
        # was running, and otherwise only cleared by an explicit
        # set_defaults call). Without this, a model that was "invalid"
        # at startup stays permanently rejected even after it's real.
        defaults_manager._invalid_models.clear()
    return result


def register_lifecycle_tools(mcp: FastMCP, comfyui_client=None, defaults_manager=None):
    """Register ComfyUI process lifecycle tools with the MCP server."""

    @mcp.tool()
    def start_comfyui() -> dict:
        """Start the ComfyUI server if it isn't already running.

        ComfyUI runs on-demand to save GPU/RAM rather than staying resident.
        run_workflow starts it automatically, so calling this first is only
        needed to warm it up ahead of time. Blocks until ComfyUI is ready.
        Safe to call even if it's already running (idempotent, just confirms
        status and re-arms the idle timer).
        """
        try:
            return start_comfyui_and_refresh(comfyui_client, defaults_manager)
        except requests.RequestException as e:
            return {"error": f"could not start ComfyUI: {e}"}

    @mcp.tool()
    def stop_comfyui() -> dict:
        """Stop the ComfyUI server to free the GPU and system memory.

        Call this once you're done generating for now. If you forget,
        ComfyUI stops itself automatically after a period of inactivity.
        """
        try:
            return _call("/stop", "POST")
        except requests.RequestException as e:
            return {"error": f"could not stop ComfyUI: {e}"}

    @mcp.tool()
    def get_comfyui_status() -> dict:
        """Check whether ComfyUI is currently running and how it's managed.

        Returns running state, whether this control layer started it (vs.
        it being started some other way), and seconds until it will
        auto-stop from inactivity if applicable.
        """
        try:
            return _call("/status", "GET")
        except requests.RequestException as e:
            return {"error": f"could not reach control API: {e}"}
