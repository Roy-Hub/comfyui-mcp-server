"""Auto-discovery bridge: when a workflow isn't already cached locally as an
MCP-server file, fetch it live from the ComfyUI control API (which converts
straight from whatever is currently saved in ComfyUI's own workflow
directory), heuristically parameterize its prompt/seed/steps, and write it
into the MCP server's workflows/ directory so the existing, already-tested
load_workflow/apply_workflow_overrides/run_custom_workflow pipeline picks it
up completely unchanged.

This means a new workflow saved in ComfyUI - or a newly downloaded model
referenced by an existing workflow's `model` override - needs zero new code
here: it becomes usable the next time it's requested by name.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("MCP_Server")

CONTROL_URL = os.getenv("COMFYUI_CONTROL_URL", "http://127.0.0.1:8189")
CONTROL_TOKEN = os.getenv("COMFYUI_CONTROL_TOKEN", "")

SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "DualModelGuider"}


def _control_api(path: str) -> dict:
    req = urllib.request.Request(f"{CONTROL_URL}{path}", headers={"X-Control-Token": CONTROL_TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _parameterize(wf: dict) -> tuple[dict, list]:
    """Same heuristic used to convert the initially-curated workflows:
    follow the sampler's positive/negative links to find CLIPTextEncode
    prompt nodes, and look for seed/steps on the sampler or the nodes it
    pulls noise/sigmas from."""
    sampler_id = next((nid for nid, node in wf.items() if node.get("class_type") in SAMPLER_TYPES), None)
    if sampler_id is None:
        return wf, []

    sampler = wf[sampler_id]
    params = []

    for role, param_name in (("positive", "PARAM_PROMPT"), ("negative", "PARAM_NEGATIVE_PROMPT")):
        link = sampler["inputs"].get(role)
        if isinstance(link, list):
            origin = wf.get(link[0])
            if origin and origin.get("class_type") == "CLIPTextEncode" and isinstance(origin["inputs"].get("text"), str):
                origin["inputs"]["text"] = param_name
                params.append(param_name)

    if isinstance(sampler["inputs"].get("seed"), (int, float)):
        sampler["inputs"]["seed"] = "PARAM_INT_SEED"
        params.append("PARAM_INT_SEED")
    else:
        noise_link = sampler["inputs"].get("noise")
        if isinstance(noise_link, list):
            origin = wf.get(noise_link[0])
            if origin and origin.get("class_type") == "RandomNoise" and "noise_seed" in origin.get("inputs", {}):
                origin["inputs"]["noise_seed"] = "PARAM_INT_SEED"
                params.append("PARAM_INT_SEED")

    if isinstance(sampler["inputs"].get("steps"), (int, float)):
        sampler["inputs"]["steps"] = "PARAM_INT_STEPS"
        params.append("PARAM_INT_STEPS")
    else:
        sigmas_link = sampler["inputs"].get("sigmas")
        if isinstance(sigmas_link, list):
            origin = wf.get(sigmas_link[0])
            if origin and isinstance(origin.get("inputs", {}).get("steps"), (int, float)):
                origin["inputs"]["steps"] = "PARAM_INT_STEPS"
                params.append("PARAM_INT_STEPS")

    return wf, params


def ensure_workflow_cached(workflow_id: str, workflows_dir) -> tuple[bool, str]:
    """If workflow_id isn't already a file in workflows_dir, try to find a
    matching ComfyUI-side saved workflow via the control API, convert +
    parameterize it, and write it in. Returns (found, message)."""
    slug = slugify(workflow_id)
    target = workflows_dir / f"{slug}.json"
    if target.exists():
        return True, "already cached"

    try:
        listing = _control_api("/workflows")
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach ComfyUI control API to look up '{workflow_id}': {e}"

    match = next((w for w in listing.get("workflows", []) if slugify(w["name"]) == slug), None)
    if match is None:
        return False, f"no ComfyUI workflow matching '{workflow_id}' found (checked control API's saved workflows)"
    if not match.get("convertible", True):
        return False, f"ComfyUI workflow '{match['name']}' exists but can't be auto-converted: {match.get('reason')}"

    try:
        data = _control_api(f"/workflows/{urllib.parse.quote(match['name'])}")
    except (urllib.error.URLError, OSError) as e:
        return False, f"found '{match['name']}' but could not fetch/convert it (is ComfyUI running? call start_comfyui first): {e}"

    wf, params = _parameterize(data["prompt"])
    if "PARAM_PROMPT" not in params:
        return False, f"found '{match['name']}' but could not identify its prompt node automatically"

    workflows_dir.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        json.dump(wf, f, indent=2)
    logger.info(f"Auto-cached new workflow '{match['name']}' -> {target} (params: {params})")
    return True, f"auto-discovered and cached '{match['name']}' as '{slug}' (params: {params})"
