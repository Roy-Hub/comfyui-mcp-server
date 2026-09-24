"""Workflow management tools for ComfyUI MCP Server"""

import logging
import random
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from tools.helpers import register_and_build_response
from tools.dynamic_workflows import ensure_workflow_cached, slugify, _control_api

logger = logging.getLogger("MCP_Server")


def register_workflow_tools(
    mcp: FastMCP,
    workflow_manager,
    comfyui_client,
    defaults_manager,
    asset_registry
):
    """Register workflow tools with the MCP server"""
    
    @mcp.tool()
    def list_workflows() -> dict:
        """List all available workflows in the workflow directory.
        
        Returns a catalog of workflows with their IDs, names, descriptions,
        available inputs, and optional metadata.
        """
        catalog = workflow_manager.get_workflow_catalog()

        # Also surface ComfyUI-side saved workflows that haven't been used
        # (and thus auto-cached) here yet, so they're discoverable before
        # their first run_workflow call.
        try:
            known_slugs = {slugify(w["id"]) for w in catalog}
            remote = _control_api("/workflows")
            for w in remote.get("workflows", []):
                slug = slugify(w["name"])
                # convertible is None (unknown) when ComfyUI isn't running -
                # list it optimistically then; only skip a confirmed False.
                if slug in known_slugs or w.get("convertible") is False:
                    continue
                catalog.append({
                    "id": slug,
                    "name": w["name"],
                    "description": (
                        f"Saved in ComfyUI, not yet used here - call "
                        f"run_workflow(workflow_id='{slug}', overrides={{'prompt': '...'}}) "
                        f"to auto-discover and register it as a full tool."
                    ),
                    "available_inputs": {},
                    "not_yet_cached": True,
                })
        except Exception as e:
            logger.warning(f"Could not check ComfyUI for additional saved workflows: {e}")

        return {
            "workflows": catalog,
            "count": len(catalog),
            "workflow_dir": str(workflow_manager.workflows_dir)
        }

    @mcp.tool()
    def run_workflow(
        workflow_id: str,
        overrides: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        return_inline_preview: bool = False
    ) -> dict:
        """Run a saved ComfyUI workflow with constrained parameter overrides.
        
        Args:
            workflow_id: The workflow ID (filename stem, e.g., "generate_image")
            overrides: Optional dict of parameter overrides (e.g., {"prompt": "a cat", "width": 1024})
            options: Optional dict of execution options (reserved for future use)
            return_inline_preview: If True, include a small thumbnail base64 in response (256px, ~100KB)
        
        Returns:
            Result with asset_url, workflow_id, and execution metadata. If return_inline_preview=True,
            also includes inline_preview_base64 for immediate viewing.
        """
        if overrides is None:
            overrides = {}

        # Normalize so a spaced/mixed-case ComfyUI display name ("Sample Txt
        # 2 Image") resolves the same way as its cached filename.
        workflow_id = slugify(workflow_id)

        # Load workflow, auto-discovering it from ComfyUI's own saved
        # workflows (via the control API) on first use if we don't already
        # have it cached locally - no code change needed to use a new
        # workflow the user saves in ComfyUI.
        workflow = workflow_manager.load_workflow(workflow_id)
        discovery_note = None
        if not workflow:
            found, msg = ensure_workflow_cached(workflow_id, workflow_manager.workflows_dir)
            if found:
                discovery_note = msg
                workflow = workflow_manager.load_workflow(workflow_id)
            if not workflow:
                return {"error": f"Workflow '{workflow_id}' not found. {msg}"}

        # apply_workflow_overrides only fills a value for an un-overridden
        # PARAM_ placeholder from defaults_manager's per-namespace hardcoded
        # defaults - which doesn't include "seed" at all (the named,
        # auto-generated tool wrappers special-case random seed generation;
        # this generic path doesn't). Without this, an un-overridden
        # PARAM_INT_SEED/PARAM_INT_STEPS gets submitted to ComfyUI as a
        # literal string and fails validation.
        parameters = workflow_manager._extract_parameters(workflow)
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

            if discovery_note:
                response["auto_discovery"] = discovery_note

            return response
        except Exception as exc:
            logger.exception("Workflow '%s' failed", workflow_id)
            return {"error": str(exc)}
