# ComfyUI MCP Server

> Generate and refine AI images/audio/video through natural conversation

A lightweight MCP (Model Context Protocol) server that lets AI agents generate and iteratively refine images, audio, and video using a local ComfyUI instance.

You run the server, connect a client, and issue tool calls. Everything else is optional depth.

> **Fork note:** This is a fork of [joenorton/comfyui-mcp-server](https://github.com/joenorton/comfyui-mcp-server). It adds:
> - on-demand ComfyUI lifecycle tools (`start_comfyui`, `stop_comfyui`, `get_comfyui_status`)
> - auto-discovery of workflows saved in ComfyUI
> - a `list_all_models` tool covering non-checkpoint loaders
> - a configurable workflow directory kept outside the repo
> - a configurable host/port (default port `9001`)
>
> See [Fork Additions](#fork-additions).

---

## Quick Start (2–3 minutes)

This proves everything is working.

### 1) Clone and set up

```bash
git clone https://github.com/Roy-Hub/comfyui-mcp-server.git
cd comfyui-mcp-server
pip install -r requirements.txt
```

### 2) Start ComfyUI

Make sure ComfyUI is installed and running locally. (Or, if you have set up the optional
[control API](#on-demand-comfyui-lifecycle), skip this step. Agents can start ComfyUI on demand with `start_comfyui`.)

```bash
cd <ComfyUI_dir>
python main.py --port 8188
```

### 3) Run the MCP server

From the repository directory:

```bash
python server.py
```

The server listens at:

```
http://127.0.0.1:9001/mcp
```

The port and bind address are set with `COMFY_MCP_PORT` (default `9001`) and `COMFY_MCP_HOST` (default `0.0.0.0`).
The server starts even if ComfyUI is not running yet.

### 4) Verify it works (no AI client required)

Run the included test client:

```bash
# Use default prompt
python test_client.py

# Or provide your own prompt
python test_client.py -p "a beautiful sunset over mountains"
python test_client.py --prompt "a cat on a mat"
```

`test_client.py` will:

* connect to the MCP server
* list available tools
* fetch and display server defaults (width, height, steps, model, etc.)
* run `generate_image` with your prompt (or a default)
* automatically use server defaults for all other parameters
* print the resulting asset information

If this step succeeds, the system is working.

**Note:** The test client respects server defaults configured via config files, environment variables, or `set_defaults` calls. Only the `prompt` parameter is required; all other parameters use server defaults automatically.

That’s it.

---

## Use with an AI Agent (Cursor / Claude / n8n)

Once the server is running, you can connect it to an AI client.

Create a project-scoped `.mcp.json` file:

```json
{
  "mcpServers": {
    "comfyui-mcp-server": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:9001/mcp"
    }
  }
}
```

**Note:** Some clients use `"type": "http"` instead of `"streamable-http"`. Both work with this server. If auto-discovery doesn't work, try changing the type field.

Restart your AI client. You can now call tools such as:

* `generate_image`
* `view_image`
* `regenerate`
* `get_job`
* `list_assets`

This is the primary intended usage mode.

---

## What You Can Do After It Works

Once you’ve confirmed the server runs and a client can connect, the system supports:

* Iterative refinement via `regenerate` (no re-prompting)
* Explicit asset identity for reliable follow-ups
* Job polling and cancellation for long-running generations
* Optional image injection into the AI’s context (`view_image`)
* Auto-discovered ComfyUI workflows with parameter exposure
* Configurable defaults to avoid repeating common settings

Everything below builds on the same basic loop you just tested.

## Migration Notes (Previous Versions)

If you’ve used earlier versions of this project, a few things have changed.

### What’s the Same
- You still run a local MCP server that delegates execution to ComfyUI
- Workflows are still JSON files placed in a workflow directory (`workflows/` by default, or `COMFY_MCP_WORKFLOW_DIR`)
- Image generation behavior is unchanged at its core

### What’s New
- **Streamable HTTP transport** replaces the older WebSocket-based approach
- **Explicit job management** (`get_job`, `get_queue_status`, `cancel_job`)
- **Asset identity** instead of ad-hoc URLs (stable across hostname changes)
- **Iteration support** via `regenerate` (replay with parameter overrides)
- **Optional visual feedback** for agents via `view_image`
- **Configurable defaults** to avoid repeating common parameters

### What Changed Conceptually
Earlier versions were a thin request/response bridge.
The current version is built around **iteration** and **stateful control loops**.

You can still generate an image with a single call, but you now have the option to:
- refer back to specific outputs
- refine results without re-specifying everything
- poll and cancel long-running jobs
- let AI agents inspect generated images directly

### Looking for the Old Behavior?
If you want the minimal, single-shot behavior from earlier versions:
- run `test_client.py` (this mirrors the original usage pattern)
- call `generate_image` with just a prompt (server defaults handle the rest)
- ignore the additional tools

No migration is required unless you want the new capabilities.

## Available Tools

### Generation Tools

- **`generate_image`**: Generate images (requires `prompt`)
- **`generate_song`**: Generate audio (requires `tags` and `lyrics`)
- **`regenerate`**: Regenerate an existing asset with optional parameter overrides (requires `asset_id`)

### Viewing Tools

- **`view_image`**: View generated images inline (images only, not audio/video)

### Job Management Tools

- **`get_queue_status`**: Check ComfyUI queue state (running/pending jobs) - provides async awareness
- **`get_job`**: Poll job completion status by prompt_id - check if a job has finished
- **`list_assets`**: Browse recently generated assets - enables AI memory and iteration
- **`get_asset_metadata`**: Get full provenance and parameters for an asset - includes workflow history
- **`cancel_job`**: Cancel a queued or running job

### Configuration Tools

- **`list_models`**: List available ComfyUI checkpoint models
- **`list_all_models`**: List installed models across all loader types: checkpoints, diffusion models (UNETLoader), text encoders (CLIPLoader), VAEs, and upscale models. Use this for UNET/CLIP/VAE-style pipelines (e.g. Flux, Z-Image-Turbo, Ideogram4)
- **`get_defaults`**: Get current default values
- **`set_defaults`**: Set default values (with optional persistence)

### Workflow Tools

- **`list_workflows`**: List all available workflows, including workflows saved in ComfyUI that have not been used yet (marked `not_yet_cached`)
- **`run_workflow`**: Run any workflow with custom parameters. Unknown workflow IDs are auto-discovered from ComfyUI (see [Workflow Auto-Discovery](#workflow-auto-discovery)). A random `seed` and `steps=20` are filled in when not provided

### Lifecycle Tools (optional, requires control API)

- **`start_comfyui`**: Start ComfyUI if it isn't running, and wait until it is ready. Idempotent. Refreshes the server's cached model lists after start
- **`stop_comfyui`**: Stop ComfyUI to free GPU/RAM
- **`get_comfyui_status`**: Report whether ComfyUI is running

### Publish Tools

- **`get_publish_info`**: Show publish status (detected project root, publish dir, ComfyUI output root, and any missing setup)
- **`set_comfyui_output_root`**: Set ComfyUI output directory (recommended for Comfy Desktop / nonstandard installs; persisted across restarts)
- **`publish_asset`**: Publish a generated asset into the project's web directory with deterministic compression (default 600KB)

**Publish Notes:**
- **Session-scoped**: `asset_id`s are valid only for the current server session; restart invalidates them.
- **Zero-config in common cases**: Publish dir auto-detected (`public/gen`, `static/gen`, or `assets/gen`); if ComfyUI output can't be detected, set it once via `set_comfyui_output_root`.
- **Two modes**: Demo (explicit filename) and Library (auto filename + manifest update). In library mode, `manifest_key` is required.
- **Manifest**: Updated only when `manifest_key` is provided.
- **Compression**: Deterministic ladder to meet size limits; fails with a clear error if it can't.

**Quick Start:**

Example agent conversation flow:

**User:** "Generate a hero image for my website and publish it as hero.webp"

**Agent:** *Checks publish configuration*
- Calls `get_publish_info()` → sees status "ready"

**Agent:** *Generates image*
- Calls `generate_image(prompt="a hero image for a website")` → gets `asset_id`

**Agent:** *Publishes asset*
- Calls `publish_asset(asset_id="...", target_filename="hero.webp")` → success

**User:** "Now generate a logo and add it to the manifest as 'site-logo'"

**Agent:** *Generates and publishes with manifest*
- Calls `generate_image(prompt="a modern logo")` → gets `asset_id`
- Calls `publish_asset(asset_id="...", manifest_key="site-logo")` → auto-generates filename, updates manifest

See [docs/HOW_TO_TEST_PUBLISH.md](docs/HOW_TO_TEST_PUBLISH.md) for detailed usage and testing instructions.

## Custom Workflows

Add custom workflows by placing JSON files in the workflow directory. Workflows are automatically discovered and exposed as MCP tools.

The workflow directory defaults to `workflows/` in this repo, which only ships example workflows. To keep your own
workflows separate from the code, point `COMFY_MCP_WORKFLOW_DIR` at a folder outside the repo:

```bash
export COMFY_MCP_WORKFLOW_DIR=~/.config/comfyui-mcp/workflows
```

When this is set, **only** that folder is scanned. Copy in any example workflows you still want (e.g. `generate_image.json`).

### Workflow Placeholders

Use `PARAM_*` placeholders in workflow JSON to expose parameters:

- `PARAM_PROMPT` → `prompt: str` (required)
- `PARAM_INT_STEPS` → `steps: int` (optional)
- `PARAM_FLOAT_CFG` → `cfg: float` (optional)

**Example:**
```json
{
  "3": {
    "inputs": {
      "text": "PARAM_PROMPT",
      "steps": "PARAM_INT_STEPS"
    }
  }
}
```

The tool name is derived from the filename (e.g., `my_workflow.json` → `my_workflow` tool).

---

## Fork Additions

### On-Demand ComfyUI Lifecycle

Instead of keeping ComfyUI resident, you can let agents start and stop it on demand. The lifecycle tools don't manage
the process themselves. They call a small, separate **control API** service (not included in this repo) that you run
alongside ComfyUI, e.g. as a systemd service. It should also stop ComfyUI after a period of inactivity.

Every request sends the header `X-Control-Token: $COMFYUI_CONTROL_TOKEN`. The control API must implement:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/start` | Start ComfyUI (if needed) and block until it is ready |
| `POST` | `/stop` | Stop ComfyUI |
| `GET` | `/status` | Return running state |
| `GET` | `/workflows` | List ComfyUI saved workflows: `{"workflows": [{"name", "convertible", "reason"}]}` |
| `GET` | `/workflows/{name}` | Return a saved workflow converted to API format: `{"prompt": {...}}` |

Without a control API, the rest of the server works as upstream. The lifecycle tools simply return errors.

### Workflow Auto-Discovery

When `run_workflow` is called with a workflow ID that is not in the workflow directory, the server:

1. Slugifies the ID (`"Sample Txt 2 Image"` → `sample_txt_2_image`) and looks for a matching workflow via the control API's `/workflows`
2. Fetches it in API format and adds placeholders automatically:
   - `PARAM_PROMPT` / `PARAM_NEGATIVE_PROMPT` on the `CLIPTextEncode` nodes linked to the sampler's positive/negative inputs
   - `PARAM_INT_SEED` on the sampler's `seed` (or `RandomNoise.noise_seed`)
   - `PARAM_INT_STEPS` on `steps`
3. Saves the result to the workflow directory, so later calls (and restarts) use the saved copy

Save a workflow in ComfyUI and it is usable by name. No code changes are needed. ComfyUI must be running
for the conversion step (call `start_comfyui` first). Review auto-generated files and adjust placeholders if the heuristic
picks the wrong node.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `COMFYUI_URL` | `http://localhost:8188` | ComfyUI server URL |
| `COMFY_MCP_HOST` | `0.0.0.0` | MCP server bind address. Set `127.0.0.1` to accept local connections only |
| `COMFY_MCP_PORT` | `9001` | MCP server port |
| `COMFY_MCP_WORKFLOW_DIR` | `./workflows` | Workflow directory (can be outside the repo) |
| `COMFYUI_CONTROL_URL` | `http://127.0.0.1:8189` | Control API URL (lifecycle + auto-discovery) |
| `COMFYUI_CONTROL_TOKEN` | *(empty)* | Shared secret sent as `X-Control-Token`. Keep it in an env file, never in the repo |

See [docs/REFERENCE.md](docs/REFERENCE.md) for the `COMFY_MCP_DEFAULT_*` and other upstream variables.

Example systemd user unit loading these from an env file:

```ini
[Service]
WorkingDirectory=%h/mcp-servers/comfyui-mcp-server
EnvironmentFile=%h/.config/comfyui-mcp/env
ExecStart=%h/mcp-servers/comfyui-mcp-server/.venv/bin/python server.py
Restart=on-failure
```

---

## Configuration

The server supports configurable defaults to avoid repeating common parameters. Defaults can be set via:

- **Runtime defaults**: Use `set_defaults` tool (ephemeral, lost on restart)
- **Config file**: `~/.config/comfy-mcp/config.json` (persistent)
- **Environment variables**: `COMFY_MCP_DEFAULT_*` prefixed variables

Defaults are resolved in priority order: per-call values → runtime defaults → config file → environment variables → hardcoded defaults.

For complete configuration details, see [docs/REFERENCE.md](docs/REFERENCE.md#parameters).

---

## Detailed Reference

Complete parameter lists, return schemas, configuration options, and advanced workflow metadata are documented in:

- **[API Reference](docs/REFERENCE.md)** - Complete tool reference, parameters, return values, and configuration
- **[Architecture](docs/ARCHITECTURE.md)** - Design decisions and system overview

## Project Structure

```
comfyui-mcp-server/
├── server.py              # Main entry point
├── comfyui_client.py      # ComfyUI API client
├── asset_processor.py     # Image processing utilities
├── test_client.py         # Test client
├── managers/              # Core managers
│   ├── workflow_manager.py
│   ├── defaults_manager.py
│   └── asset_registry.py
├── tools/                 # MCP tool implementations
│   ├── generation.py
│   ├── asset.py
│   ├── job.py             # Job management tools
│   ├── configuration.py
│   ├── lifecycle.py       # start/stop/status via control API
│   ├── dynamic_workflows.py  # workflow auto-discovery
│   ├── publish.py
│   └── workflow.py
├── models/                # Data models
│   ├── workflow.py
│   └── asset.py
└── workflows/             # Example workflows (default COMFY_MCP_WORKFLOW_DIR)
    ├── basic_api_test.json
    ├── generate_image.json
    └── generate_song.json
```

## Notes

- The server binds to `0.0.0.0` by default (all interfaces), so it is reachable from your LAN. Set `COMFY_MCP_HOST=127.0.0.1` for local-only access. Do not expose it publicly without authentication or a reverse proxy.
- Ensure your models exist in the matching `<ComfyUI_dir>/models/` subfolder (`checkpoints/`, `diffusion_models/`, `text_encoders/`, `vae/`, `upscale_models/`)
- Server uses **streamable-http** transport (HTTP-based, not WebSocket)
- Workflows are auto-discovered - no code changes needed
- Assets expire after 24 hours (configurable)
- `view_image` only supports images (PNG, JPEG, WebP, GIF)
- Asset identity uses `(filename, subfolder, type)` instead of URL for robustness
- Full workflow history is stored for provenance and reproducibility
- `regenerate` uses stored workflow data to recreate assets with parameter overrides
- Session isolation: `list_assets` can filter by session for clean AI agent context

## Troubleshooting

**Server won't start:**
- The server no longer requires ComfyUI at boot. If it is offline, a notice is printed and you can call `start_comfyui` later
- Check port `9001` isn't already in use (change with `COMFY_MCP_PORT`)
- Verify Python 3.8+ is installed (`python --version`)
- Check all dependencies are installed: `pip install -r requirements.txt`
- Check server logs for specific error messages

**Client can't connect:**
- Verify the server prints "Endpoint: http://127.0.0.1:9001/mcp" in the console
- Test server directly: `curl http://127.0.0.1:9001/mcp` (should return MCP response)
- Check `.mcp.json` is in project root (or correct location for your client)
- Try both `"type": "streamable-http"` and `"type": "http"` - both are supported
- For Cursor-specific issues, see [docs/MCP_CONFIG_README.md](docs/MCP_CONFIG_README.md)

**Tools not appearing:**
- Check the workflow directory (`COMFY_MCP_WORKFLOW_DIR`, default `workflows/`) has JSON files with `PARAM_*` placeholders
- Check server logs for workflow parsing errors
- Verify ComfyUI has required custom nodes installed (if using custom workflows)
- Restart the MCP server after adding new workflows (auto-discovered workflows work via `run_workflow` immediately, and become named tools after a restart)

**Lifecycle tools / auto-discovery fail:**
- Check the control API is running at `COMFYUI_CONTROL_URL`
- Check `COMFYUI_CONTROL_TOKEN` matches the control API's token
- Auto-discovery needs ComfyUI running to convert workflows. Call `start_comfyui` first

**Asset not found errors:**
- Assets expire after 24 hours by default (configurable via `COMFY_MCP_ASSET_TTL_HOURS`)
- Assets are lost on server restart (ephemeral by design)
- Use `get_asset_metadata` to verify asset exists before using `regenerate`
- Check server logs to see if asset was registered successfully

## Known Limitations (v1.0)

- **Ephemeral asset registry**: `asset_id` references are only valid while the MCP server is running (and until TTL expiry). After restart, previously-issued `asset_id`s can’t be resolved, and regenerate will fail for those assets.

## Contributing

Issues and pull requests are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## Acknowledgements

- [@venetanji](https://github.com/venetanji) - streamable-http foundation & PARAM_* system

## Maintainer
[@joenorton](https://github.com/joenorton)

## License

Apache License 2.0
