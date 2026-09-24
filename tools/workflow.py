"""Workflow management tools for ComfyUI MCP Server"""

import logging
import random
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from mcp.server.fastmcp import FastMCP
from tools.helpers import register_and_build_response
from tools.dynamic_workflows import (
    ControlAPIError,
    META_SUFFIX,
    preview_inputs,
    slugify,
    sync_workflows,
    _read_meta,
)
from tools.lifecycle import comfyui_running, start_comfyui_and_refresh

logger = logging.getLogger("MCP_Server")

USAGE = ("Pick a workflow by its description, write a prompt in the style of its sample_prompt, "
         "then call run_workflow(workflow_id=<id>, overrides={'prompt': '...'}). "
         "Other inputs are optional and default to the values the workflow was saved with.")


def register_workflow_tools(
    mcp: FastMCP,
    workflow_manager,
    comfyui_client,
    defaults_manager,
    asset_registry
):
    """Register workflow tools with the MCP server"""

    def _running_or_none() -> Optional[bool]:
        try:
            return comfyui_running()
        except requests.RequestException as e:
            logger.warning("Could not reach control API for ComfyUI status: %s", e)
            return None

    def _sync(comfy_running: bool):
        """Returns ({slug: meta}, None) or (None, error) if the control API
        is unreachable - in which case the local cache is used untouched."""
        try:
            return sync_workflows(workflow_manager.workflows_dir, comfy_running), None
        except ControlAPIError as e:
            logger.warning("Workflow sync skipped: %s", e)
            return None, str(e)

    def _local_meta(workflow_id: str) -> dict:
        return _read_meta(Path(workflow_manager.workflows_dir) / f"{workflow_id}{META_SUFFIX}") or {}

    def _entry(workflow_id: str, meta: dict, local: Optional[dict]) -> dict:
        summary = meta.get("summary") or {}
        status = meta.get("status", "ready")
        entry = {
            "id": workflow_id,
            "name": meta.get("name") or (local or {}).get("name") or workflow_id,
            "description": meta.get("description") or (local or {}).get("description", ""),
            "status": status,
        }
        if status == "ready" and local is not None:
            entry["inputs"] = local["available_inputs"]
        elif status in ("ready", "preview"):
            entry["inputs"] = preview_inputs(summary)
            entry["note"] = ("ComfyUI is idle, so these inputs are a preview. "
                             "run_workflow starts ComfyUI and works as-is.")
        else:
            entry["reason"] = meta.get("reason")
            entry["note"] = "This workflow can't be run until the reason above is fixed in ComfyUI."
        if meta.get("defaults"):
            entry["defaults"] = meta["defaults"]
        if summary.get("sample_prompt"):
            entry["sample_prompt"] = summary["sample_prompt"]
        if summary.get("sample_negative_prompt"):
            entry["sample_negative_prompt"] = summary["sample_negative_prompt"]
        return entry

    @mcp.tool()
    def list_workflows() -> dict:
        """List the workflows you can run with run_workflow.

        Always reflects the workflows currently saved in ComfyUI: newly saved
        ones appear and deleted ones disappear on every call. Each entry has:
        - id: pass to run_workflow
        - description: output type, size, models, and prompt format
        - inputs: what run_workflow accepts in overrides
        - sample_prompt: the prompt the workflow was saved with - write new
          prompts in the same style/format (some expect structured JSON)
        - status: "ready", "preview" (ComfyUI idle; still runnable), or
          "not_convertible" (see reason)
        Does not start ComfyUI.
        """
        running = _running_or_none()
        synced, error = _sync(bool(running))
        local = {w["id"]: w for w in workflow_manager.get_workflow_catalog()}

        workflows = []
        for workflow_id, meta in (synced or {}).items():
            workflows.append(_entry(workflow_id, meta, local.get(workflow_id)))
        for workflow_id, w in local.items():
            if synced is not None and workflow_id in synced:
                continue
            workflows.append(_entry(workflow_id, _local_meta(workflow_id), w))

        response = {
            "workflows": workflows,
            "count": len(workflows),
            "comfyui_running": running,
            "usage": USAGE,
        }
        if error:
            response["warning"] = f"Showing cached workflows only - could not check ComfyUI: {error}"
        return response

    @mcp.tool()
    def run_workflow(
        workflow_id: str,
        overrides: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        return_inline_preview: bool = False
    ) -> dict:
        """Run a workflow from list_workflows. Starts ComfyUI automatically if it is idle.

        Args:
            workflow_id: The workflow id from list_workflows (its ComfyUI name also works)
            overrides: Inputs from list_workflows, e.g. {"prompt": "a cat"}. Omitted inputs
                use the workflow's saved defaults (seed is random unless the workflow fixes it).
            options: Optional dict of execution options (reserved for future use)
            return_inline_preview: If True, include a small thumbnail base64 in response (256px, ~100KB)

        Returns:
            Result with asset_id, asset_url, and execution metadata. If return_inline_preview=True,
            also includes inline_preview_base64 for immediate viewing.
        """
        if overrides is None:
            overrides = {}

        # Accept a ComfyUI display name ("Sample Txt 2 Image") as well as its id.
        workflow_id = slugify(workflow_id)

        started = False
        try:
            if not comfyui_running():
                start_comfyui_and_refresh(comfyui_client, defaults_manager)
                started = True
        except requests.RequestException as e:
            logger.warning("Could not check/start ComfyUI via control API: %s", e)

        # Picks up workflows added or edited in ComfyUI since the last call.
        synced, _ = _sync(True)
        meta = (synced or {}).get(workflow_id) or _local_meta(workflow_id)
        if meta.get("status") in ("not_convertible", "error"):
            return {"error": f"Workflow '{workflow_id}' can't be run: {meta.get('reason')}"}

        workflow = workflow_manager.load_workflow(workflow_id)
        if not workflow:
            available = sorted({*(synced or {}), *(w["id"] for w in workflow_manager.get_workflow_catalog())})
            return {"error": f"Workflow '{workflow_id}' not found. Available: {available}"}

        # Fill un-overridden placeholders with the workflow's saved values;
        # otherwise PARAM_INT_SEED/PARAM_INT_STEPS would reach ComfyUI as
        # literal strings and fail validation.
        parameters = workflow_manager._extract_parameters(workflow)
        defaults = meta.get("defaults") or {}
        for name in parameters:
            if name not in overrides and name in defaults:
                overrides = {**overrides, name: defaults[name]}
        if "seed" in parameters and "seed" not in overrides:
            overrides = {**overrides, "seed": random.randint(0, 2**32 - 1)}
        if "steps" in parameters and "steps" not in overrides:
            overrides = {**overrides, "steps": 20}

        try:
            # Apply overrides with constraints
            workflow = workflow_manager.apply_workflow_overrides(
                workflow, workflow_id, overrides, defaults_manager
            )

            # Extract and remove override report before submitting to ComfyUI
            override_report = workflow.pop("__override_report__", None)

            # Determine output preferences
            output_preferences = workflow_manager._guess_output_preferences(workflow)

            # Execute workflow
            result = comfyui_client.run_custom_workflow(
                workflow,
                preferred_output_keys=output_preferences,
            )

            # Register asset and build response
            response = register_and_build_response(
                result,
                workflow_id,
                asset_registry,
                tool_name=None,
                return_inline_preview=return_inline_preview,
                session_id=None
            )

            # Include override report so the agent can see what was applied/dropped
            if override_report and override_report.get("overrides_dropped"):
                response["overrides_applied"] = override_report["overrides_applied"]
                response["overrides_dropped"] = override_report["overrides_dropped"]

            if started:
                response["comfyui_started"] = True

            return response
        except Exception as exc:
            logger.exception("Workflow '%s' failed", workflow_id)
            return {"error": str(exc)}
