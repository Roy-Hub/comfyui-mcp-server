"""Tests for ComfyUI workflow sync (tools/dynamic_workflows.py)"""
import copy
import json

import pytest

from tools import dynamic_workflows as dw


def _guider_api_graph():
    """API-format graph shaped like an Ideogram workflow with a custom noise hack."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "ideogram4_fp8_scaled.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "a poster", "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["2", 0]}},
        "6": {"class_type": "DualModelGuider", "inputs": {"positive": ["4", 0], "negative": ["5", 0], "cfg": 3}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": 444}},
        "10": {"class_type": "mrmth_NoiseMathNode", "inputs": {"Noise": "a*2", "a": ["9", 0]}},
        "11": {"class_type": "Ideogram4Scheduler", "inputs": {"steps": 24, "width": 864, "height": 1120}},
        "13": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["10", 0], "guider": ["6", 0], "sigmas": ["11", 0]}},
    }


def test_parameterize_guider_graph_keeps_other_nodes():
    original = _guider_api_graph()
    wf, params, saved = dw._parameterize(copy.deepcopy(original))

    assert set(params) == {"PARAM_PROMPT", "PARAM_NEGATIVE_PROMPT", "PARAM_INT_SEED", "PARAM_INT_STEPS"}
    assert saved == {"prompt": "a poster", "negative_prompt": "blurry", "seed": 444, "steps": 24}
    assert wf["9"]["inputs"]["noise_seed"] == "PARAM_INT_SEED"
    assert wf["11"]["inputs"]["steps"] == "PARAM_INT_STEPS"
    # Everything else, including the noise hack, is untouched.
    for node_id in ("1", "6", "10", "13"):
        assert wf[node_id] == original[node_id]
    assert {k: v for k, v in wf["11"]["inputs"].items() if k != "steps"} == {"width": 864, "height": 1120}


def _ui_graph(seed_mode="fixed"):
    return {
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["model-a.safetensors"]},
            {"id": 2, "type": "CLIPTextEncode", "widgets_values": ["a red fox"], "inputs": []},
            {"id": 3, "type": "CLIPTextEncode", "widgets_values": ["low quality"], "inputs": []},
            {"id": 4, "type": "EmptyLatentImage", "widgets_values": [1024, 512, 1]},
            {"id": 5, "type": "KSampler", "widgets_values": [7, seed_mode, 30, 7, "euler", "normal", 1],
             "inputs": [{"name": "positive", "link": 10}, {"name": "negative", "link": 11}]},
            {"id": 6, "type": "SaveImage", "widgets_values": ["out"]},
        ],
        "links": [[10, 2, 0, 5, 1, "CONDITIONING"], [11, 3, 0, 5, 2, "CONDITIONING"]],
    }


def test_summarize_graph():
    summary = dw.summarize_graph(_ui_graph())
    assert summary["models"] == ["model-a.safetensors"]
    assert summary["output_size"] == "1024x512"
    assert summary["outputs"] == ["image"]
    assert summary["sample_prompt"] == "a red fox"
    assert summary["sample_negative_prompt"] == "low quality"
    assert summary["fixed_seed"] == 7
    assert dw.summarize_graph(_ui_graph("randomize"))["fixed_seed"] is None


def test_describe_flags_json_prompts():
    summary = dw.summarize_graph(_ui_graph())
    assert "JSON" not in dw.describe(summary)
    summary["sample_prompt"] = '{"high_level_description": "x"}'
    assert "structured JSON prompt" in dw.describe(summary)


class FakeControlAPI:
    """Serves a set of saved ComfyUI workflows the way the control API does."""

    def __init__(self):
        self.saved = {}  # name -> (mtime, ui_graph, api_graph or None if not convertible)
        self.down = False

    def __call__(self, path, timeout=30):
        if self.down:
            raise dw.ControlAPIError("could not reach ComfyUI control API")
        if path == "/workflows":
            return {"workflows": [
                {"name": n, "mtime": m, "convertible": api is not None,
                 **({} if api is not None else {"reason": "unsupported node types: ['<subgraph>']"})}
                for n, (m, _, api) in self.saved.items()]}
        name, _, query = path[len("/workflows/"):].partition("?")
        name = dw.urllib.parse.unquote(name)
        mtime, ui, api = self.saved[name]
        if query == "raw=1":
            return {"name": name, "mtime": mtime, "graph": ui}
        return {"name": name, "mtime": mtime, "prompt": copy.deepcopy(api)}


@pytest.fixture
def control(monkeypatch):
    fake = FakeControlAPI()
    monkeypatch.setattr(dw, "_control_api", fake)
    return fake


def test_sync_adds_refreshes_and_prunes(tmp_path, control):
    control.saved["Book Cover"] = (1.0, _ui_graph(), _guider_api_graph())
    control.saved["Fancy Template"] = (1.0, _ui_graph(), None)
    (tmp_path / "my_local.json").write_text("{}")  # hand-placed, never pruned

    # Idle: listed as previews, nothing converted yet.
    result = dw.sync_workflows(tmp_path, comfy_running=False)
    assert result["book_cover"]["status"] == "preview"
    assert result["fancy_template"]["status"] == "not_convertible"
    assert "Unpack Subgraph" in result["fancy_template"]["reason"]
    assert not (tmp_path / "book_cover.json").exists()

    # Running: converted, with the saved values as defaults.
    result = dw.sync_workflows(tmp_path, comfy_running=True)
    assert result["book_cover"]["status"] == "ready"
    assert result["book_cover"]["defaults"] == {"steps": 24, "negative_prompt": "blurry", "seed": 444}
    cached = json.loads((tmp_path / "book_cover.json").read_text())
    assert cached["10"]["inputs"] == {"Noise": "a*2", "a": ["9", 0]}

    # Edited in ComfyUI: re-converted.
    edited = _guider_api_graph()
    edited["11"]["inputs"]["steps"] = 30
    control.saved["Book Cover"] = (2.0, _ui_graph(), edited)
    result = dw.sync_workflows(tmp_path, comfy_running=True)
    assert result["book_cover"]["defaults"]["steps"] == 30

    # Control API down: raises, cache left alone.
    control.down = True
    with pytest.raises(dw.ControlAPIError):
        dw.sync_workflows(tmp_path, comfy_running=True)
    assert (tmp_path / "book_cover.json").exists()

    # Deleted in ComfyUI: pruned; the local workflow stays.
    control.down = False
    del control.saved["Book Cover"]
    result = dw.sync_workflows(tmp_path, comfy_running=True)
    assert "book_cover" not in result
    assert not (tmp_path / "book_cover.json").exists()
    assert not (tmp_path / "book_cover.meta.json").exists()
    assert (tmp_path / "my_local.json").exists()


def test_edit_while_idle_removes_stale_copy(tmp_path, control):
    control.saved["Book Cover"] = (1.0, _ui_graph(), _guider_api_graph())
    dw.sync_workflows(tmp_path, comfy_running=True)
    assert (tmp_path / "book_cover.json").exists()

    control.saved["Book Cover"] = (2.0, _ui_graph(), _guider_api_graph())
    result = dw.sync_workflows(tmp_path, comfy_running=False)
    assert result["book_cover"]["status"] == "preview"
    assert not (tmp_path / "book_cover.json").exists()


def test_workflow_manager_ignores_sync_sidecars(tmp_path):
    """Startup must not treat <id>.meta.json sidecars as workflows."""
    from managers.workflow_manager import WorkflowManager

    (tmp_path / "book_cover.json").write_text(json.dumps(dw._parameterize(_guider_api_graph())[0]))
    (tmp_path / "book_cover.meta.json").write_text(json.dumps({"source": "comfyui", "status": "ready"}))
    manager = WorkflowManager(tmp_path)
    assert [d.workflow_id for d in manager.tool_definitions] == ["book_cover"]
    assert [w["id"] for w in manager.get_workflow_catalog()] == ["book_cover"]
