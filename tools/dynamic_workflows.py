"""Keeps the MCP workflow catalog in sync with the workflows saved in ComfyUI.

ComfyUI's own saved workflows (read through the control API) are the source
of truth. Each one is converted to API format, its prompt/seed/steps are
turned into PARAM_ placeholders, and the result is cached in the MCP
workflow directory next to a `<slug>.meta.json` recording where it came
from. The existing load_workflow/apply_workflow_overrides/run_custom_workflow
pipeline then runs the cached copy unchanged.

sync_workflows() reconciles that cache on every call:
- new ComfyUI workflows appear (as a preview while ComfyUI is idle, since
  conversion needs its /object_info; fully converted once it is running)
- edited workflows (mtime changed) are re-converted
- workflows deleted from ComfyUI are pruned from the cache

Summaries and sample prompts come from the raw saved graph, which the control
API serves without ComfyUI running, so listing never has to start it.
"""
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("MCP_Server")

CONTROL_URL = os.getenv("COMFYUI_CONTROL_URL", "http://127.0.0.1:8189")
CONTROL_TOKEN = os.getenv("COMFYUI_CONTROL_TOKEN", "")

SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "DualModelGuider"}
META_SUFFIX = ".meta.json"
SOURCE_COMFYUI = "comfyui"
MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft")
SAMPLE_PROMPT_MAX_CHARS = 1500
OUTPUT_KINDS = {
    "SaveImage": "image",
    "SaveAnimatedWEBP": "video",
    "SaveAnimatedPNG": "video",
    "SaveVideo": "video",
    "SaveWEBM": "video",
    "VHS_VideoCombine": "video",
    "SaveAudio": "audio",
    "SaveAudioMP3": "audio",
    "SaveAudioOpus": "audio",
}
UPSCALE_NODE_TYPES = {"UpscaleModelLoader", "ImageUpscaleWithModel"}

# list_workflows and run_workflow can overlap; serialize cache writes/prunes.
_sync_lock = threading.Lock()


class ControlAPIError(Exception):
    pass


def _control_api(path: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(f"{CONTROL_URL}{path}", headers={"X-Control-Token": CONTROL_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # The control API puts the useful reason (e.g. unsupported node
        # types) in the JSON body of its 4xx responses.
        try:
            detail = json.load(e).get("error", str(e))
        except (ValueError, AttributeError):
            detail = str(e)
        raise ControlAPIError(detail) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ControlAPIError(f"could not reach ComfyUI control API: {e}") from e


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# --------------------------------------------------------------------------
# API-format parameterization
# --------------------------------------------------------------------------

def _parameterize(wf: dict) -> tuple[dict, list, dict]:
    """Follow the sampler's positive/negative links to find the
    CLIPTextEncode prompt nodes, and look for seed/steps on the sampler or
    the nodes it pulls noise/sigmas from. Guider-style graphs (e.g.
    DualModelGuider + SamplerCustomAdvanced) keep noise and sigmas on a
    different node than positive/negative, so fall back to any RandomNoise /
    scheduler node for seed and steps.

    Only those four inputs are touched - every other node (custom noise
    handling, samplers, guiders, sigmas) is kept exactly as saved. Returns
    the values that were replaced, so they can stay the defaults."""
    sampler_id = next((nid for nid, node in wf.items() if node.get("class_type") in SAMPLER_TYPES), None)
    if sampler_id is None:
        return wf, [], {}

    sampler = wf[sampler_id]
    params = []
    saved = {}

    def _replace(inputs: dict, key: str, placeholder: str, saved_as: str):
        saved[saved_as] = inputs[key]
        inputs[key] = placeholder
        params.append(placeholder)

    for role, param_name, saved_as in (("positive", "PARAM_PROMPT", "prompt"),
                                       ("negative", "PARAM_NEGATIVE_PROMPT", "negative_prompt")):
        link = sampler["inputs"].get(role)
        if isinstance(link, list):
            origin = wf.get(link[0])
            if origin and origin.get("class_type") == "CLIPTextEncode" and isinstance(origin["inputs"].get("text"), str):
                _replace(origin["inputs"], "text", param_name, saved_as)

    if isinstance(sampler["inputs"].get("seed"), (int, float)):
        _replace(sampler["inputs"], "seed", "PARAM_INT_SEED", "seed")
    else:
        noise_link = sampler["inputs"].get("noise")
        if isinstance(noise_link, list):
            origin = wf.get(noise_link[0])
            if origin and origin.get("class_type") == "RandomNoise" and "noise_seed" in origin.get("inputs", {}):
                _replace(origin["inputs"], "noise_seed", "PARAM_INT_SEED", "seed")

    if isinstance(sampler["inputs"].get("steps"), (int, float)):
        _replace(sampler["inputs"], "steps", "PARAM_INT_STEPS", "steps")
    else:
        sigmas_link = sampler["inputs"].get("sigmas")
        if isinstance(sigmas_link, list):
            origin = wf.get(sigmas_link[0])
            if origin and isinstance(origin.get("inputs", {}).get("steps"), (int, float)):
                _replace(origin["inputs"], "steps", "PARAM_INT_STEPS", "steps")

    if "PARAM_INT_SEED" not in params:
        for node in wf.values():
            inputs = node.get("inputs", {})
            if node.get("class_type") == "RandomNoise" and isinstance(inputs.get("noise_seed"), (int, float)):
                _replace(inputs, "noise_seed", "PARAM_INT_SEED", "seed")
                break

    if "PARAM_INT_STEPS" not in params:
        for node in wf.values():
            inputs = node.get("inputs", {})
            if "Scheduler" in node.get("class_type", "") and isinstance(inputs.get("steps"), (int, float)):
                _replace(inputs, "steps", "PARAM_INT_STEPS", "steps")
                break

    return wf, params, saved


# --------------------------------------------------------------------------
# Raw (UI-format) graph summary - works without ComfyUI running
# --------------------------------------------------------------------------

def _graph_scopes(graph: dict):
    """Yield (nodes, links) for the top level and each subgraph. Top-level
    links are lists [id, origin_id, origin_slot, target_id, target_slot,
    type]; subgraph links are dicts with id/origin_id keys."""
    yield graph.get("nodes") or [], graph.get("links") or []
    for sub in (graph.get("definitions") or {}).get("subgraphs") or []:
        yield sub.get("nodes") or [], sub.get("links") or []


def _link_origins(links) -> Dict[Any, Any]:
    origins = {}
    for link in links:
        if isinstance(link, list) and len(link) >= 2:
            origins[link[0]] = link[1]
        elif isinstance(link, dict) and "id" in link:
            origins[link["id"]] = link.get("origin_id")
    return origins


def _truncate(text: str) -> str:
    text = text.strip()
    return text if len(text) <= SAMPLE_PROMPT_MAX_CHARS else text[:SAMPLE_PROMPT_MAX_CHARS] + "…"


def summarize_graph(graph: dict) -> dict:
    """Pull what an agent needs to pick and prompt a workflow out of a saved
    ComfyUI graph: models, output kind and size, upscaling, and the prompt
    text it was saved with (as a sample of the expected prompt style)."""
    models, outputs = [], []
    output_size = None
    fixed_seed = None
    upscale = False
    uses_subgraphs = bool((graph.get("definitions") or {}).get("subgraphs"))
    positive, negative, other = [], [], []

    for nodes, links in _graph_scopes(graph):
        origins = _link_origins(links)
        positive_ids, negative_ids = set(), set()
        for node in nodes:
            for inp in node.get("inputs") or []:
                origin = origins.get(inp.get("link"))
                if inp.get("name") == "positive":
                    positive_ids.add(origin)
                elif inp.get("name") == "negative":
                    negative_ids.add(origin)

        for node in nodes:
            node_type = node.get("type") or ""
            widgets = node.get("widgets_values")
            widgets = widgets if isinstance(widgets, list) else []

            for value in widgets:
                if isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS) and value not in models:
                    models.append(value)
            if node_type in OUTPUT_KINDS and OUTPUT_KINDS[node_type] not in outputs:
                outputs.append(OUTPUT_KINDS[node_type])
            if node_type in UPSCALE_NODE_TYPES:
                upscale = True
            # Seed widgets are [seed, control_after_generate]; honor "fixed"
            # the way ComfyUI does, otherwise each run gets a new seed.
            if (node_type in ("KSampler", "KSamplerAdvanced", "RandomNoise") and len(widgets) >= 2
                    and isinstance(widgets[0], int) and widgets[1] == "fixed" and fixed_seed is None):
                fixed_seed = widgets[0]
            if (output_size is None and node_type.startswith("Empty") and "Latent" in node_type
                    and len(widgets) >= 2 and all(isinstance(v, int) for v in widgets[:2])):
                output_size = f"{widgets[0]}x{widgets[1]}"
            if node_type == "CLIPTextEncode" and widgets and isinstance(widgets[0], str) and widgets[0].strip():
                node_id = node.get("id")
                bucket = negative if node_id in negative_ids else positive if node_id in positive_ids else other
                bucket.append(widgets[0])

    # Unlinked prompt nodes (e.g. inside guider graphs) - the longest is
    # almost always the main prompt rather than a short negative.
    if not positive and other:
        positive = [max(other, key=len)]

    return {
        "outputs": outputs or ["image"],
        "output_size": output_size,
        "models": models,
        "upscale": upscale,
        "fixed_seed": fixed_seed,
        "uses_subgraphs": uses_subgraphs,
        "sample_prompt": _truncate(positive[0]) if positive else None,
        "sample_negative_prompt": _truncate(negative[0]) if negative else None,
    }


def describe(summary: dict) -> str:
    parts = [f"{'/'.join(summary['outputs']).capitalize()} workflow"]
    if summary.get("output_size"):
        parts.append(f"base size {summary['output_size']}")
    if summary.get("upscale"):
        parts.append("upscaled with a model after generation")
    if summary.get("models"):
        parts.append("models: " + ", ".join(summary["models"]))
    text = "; ".join(parts) + "."
    sample = summary.get("sample_prompt") or ""
    if sample.lstrip().startswith("{"):
        text += " Expects a structured JSON prompt - follow the format of sample_prompt."
    return text


def preview_inputs(summary: dict) -> dict:
    """Best guess at inputs before the workflow can be converted (ComfyUI
    idle). Exact inputs replace this once it has been converted."""
    inputs = {"prompt": {"type": "str", "required": True, "description": "Main text prompt."}}
    if summary.get("sample_negative_prompt"):
        inputs["negative_prompt"] = {"type": "str", "required": False, "description": "Negative prompt."}
    inputs["seed"] = {"type": "int", "required": False, "description": "Random seed. Random if omitted."}
    inputs["steps"] = {"type": "int", "required": False, "description": "Sampling steps."}
    return inputs


# --------------------------------------------------------------------------
# Cache reconciliation
# --------------------------------------------------------------------------

def _meta_path(workflows_dir: Path, slug: str) -> Path:
    return workflows_dir / f"{slug}{META_SUFFIX}"


def _read_meta(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _remove_cached(workflows_dir: Path, slug: str) -> None:
    for path in (workflows_dir / f"{slug}.json", _meta_path(workflows_dir, slug)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _managed_slugs(workflows_dir: Path) -> Dict[str, dict]:
    """Cached workflows this module created (vs. hand-placed local ones)."""
    managed = {}
    if not workflows_dir.exists():
        return managed
    for meta_path in workflows_dir.glob(f"*{META_SUFFIX}"):
        meta = _read_meta(meta_path)
        if meta and meta.get("source") == SOURCE_COMFYUI:
            managed[meta_path.name[: -len(META_SUFFIX)]] = meta
    return managed


def sync_workflows(workflows_dir: Path, comfy_running: bool) -> Dict[str, dict]:
    """Reconcile the cache with ComfyUI's saved workflows and return
    {slug: meta} for every one of them. Raises ControlAPIError if the
    control API can't be reached - callers must then leave the cache alone
    (never prune on an outage)."""
    workflows_dir = Path(workflows_dir)
    listing = _control_api("/workflows").get("workflows", [])

    with _sync_lock:
        workflows_dir.mkdir(parents=True, exist_ok=True)
        remote = {}
        for w in listing:
            remote.setdefault(slugify(w["name"]), w)

        for slug in _managed_slugs(workflows_dir):
            if slug not in remote:
                _remove_cached(workflows_dir, slug)
                logger.info("Pruned workflow '%s' (deleted from ComfyUI)", slug)

        result = {}
        for slug, w in remote.items():
            try:
                result[slug] = _sync_one(workflows_dir, slug, w, comfy_running)
            except ControlAPIError as e:
                logger.warning("Could not sync workflow '%s': %s", w["name"], e)
                result[slug] = {"source": SOURCE_COMFYUI, "source_name": w["name"], "status": "error",
                                "reason": str(e)}
        return result


def _is_current(meta: dict, w: dict, workflow_path: Path, comfy_running: bool) -> bool:
    status = meta.get("status")
    if status == "ready":
        return workflow_path.exists()
    if status == "preview":
        return not comfy_running
    if status == "not_convertible":
        # Re-check if ComfyUI now says it converts (e.g. a custom node was installed).
        return w.get("convertible") is not True
    return False


def _sync_one(workflows_dir: Path, slug: str, w: dict, comfy_running: bool) -> dict:
    meta_path = _meta_path(workflows_dir, slug)
    workflow_path = workflows_dir / f"{slug}.json"
    meta = _read_meta(meta_path)
    unchanged = bool(meta) and meta.get("source") == SOURCE_COMFYUI and meta.get("source_mtime") == w.get("mtime")
    if unchanged and _is_current(meta, w, workflow_path, comfy_running):
        return meta

    name = w["name"]
    quoted = urllib.parse.quote(name)
    if unchanged and meta.get("summary"):
        summary = meta["summary"]
    else:
        summary = summarize_graph(_control_api(f"/workflows/{quoted}?raw=1").get("graph", {}))

    meta = {
        "source": SOURCE_COMFYUI,
        "source_name": name,
        "source_mtime": w.get("mtime"),
        "name": name,
        "description": describe(summary),
        "summary": summary,
    }

    if w.get("convertible") is False:
        meta.update(status="not_convertible", reason=_explain(w.get("reason")))
    elif not comfy_running:
        # Can't convert without ComfyUI; don't leave an outdated copy runnable.
        try:
            workflow_path.unlink()
        except FileNotFoundError:
            pass
        meta["status"] = "preview"
    else:
        try:
            api = _control_api(f"/workflows/{quoted}").get("prompt", {})
            wf, params, saved = _parameterize(api)
            if "PARAM_PROMPT" in params:
                _write_json(workflow_path, wf)
                meta["status"] = "ready"
                # Values the workflow was saved with stay the defaults, so
                # an agent that only passes a prompt gets the tuned result.
                defaults = {k: saved[k] for k in ("steps", "negative_prompt") if k in saved}
                if "seed" in saved and summary.get("fixed_seed") is not None:
                    defaults["seed"] = saved["seed"]
                meta["defaults"] = defaults
                logger.info("Synced workflow '%s' -> %s (params: %s)", name, workflow_path.name, params)
            else:
                meta.update(status="not_convertible", reason="could not identify the prompt node automatically")
        except ControlAPIError as e:
            meta.update(status="not_convertible", reason=_explain(str(e)))

    _write_json(meta_path, meta)
    return meta


def _explain(reason: Optional[str]) -> Optional[str]:
    if reason and "<subgraph>" in reason:
        return (f"{reason}. Subgraphs can't be converted yet - open the workflow in ComfyUI, "
                "right-click the subgraph node -> Unpack Subgraph, and save.")
    return reason
