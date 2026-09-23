# LTX Chain Controller for ComfyUI

A custom ComfyUI node pack that renders longer LTX video projects as sequential blocks and joins them into one final video. It is designed for projects with a timeline, audio-driven generation, and consistent visual continuity across block boundaries.

The pack contains two nodes:

* **LTX Chain Controller** – renders the timeline block by block, handles the handoff between blocks, and assembles the final video.
* **LTX Resolution Selector** – central configuration node that picks the working resolution and provides `window_frames` / `fps` values for the rest of the graph.

✨ Features
----------

* **Long-video block rendering:** Splits a timeline into manageable sequential blocks instead of generating the full duration in a single pass.
* **Three continuity modes:** Choose between `keyframe`, `latent`, and `full` handoff behavior.
* **Reference strategies:** Continue from the previous block (`chained`) or hold a fixed visual anchor (`fixed_first` / `fixed_custom`).
* **Stable keyframe workflow:** `keyframe` + `chained` uses the last frame of the prior block as the reference for the next block.
* **Fixed seed option:** Allows deterministic block generation when a fixed seed base is selected.
* **Latent and frame persistence:** Saves block latents and last-frame images for later blocks when the selected mode requires them.
* **Cleanup and dry run:** `cleanup_now` removes stored latents and last-frame references after cancelled or abandoned runs; `dry_run` computes and logs every block window without queueing anything.
* **Resolution passthrough:** Optional `width_in` / `height_in` INT sockets accept the resolution from the LTX Resolution Selector and enforce one resolution across all blocks.
* **Central resolution node:** The LTX Resolution Selector offers a direct resolution dropdown plus `window_frames` / `fps` inputs and exposes all four values as outputs.
* **Final assembly:** Concatenates completed block videos and muxes the project audio into `final.mp4` through FFmpeg, trimming overlap frames to keep audio in sync.

🧩 Nodes
--------

### LTX Chain Controller

The controller is a pure output node: it drives the chain and produces the final video, but it has **no result outputs**. All settings are configured through its widgets and optional inputs.

#### Widgets

| Widget | Type / Default | Purpose |
| --- | --- | --- |
| `block_index` | INT, `0` | Index of the block this node instance finishes. The controller increments it and enqueues the next block. |
| `total_blocks` | INT, `0` | Number of blocks to render. `0` = auto: derived from `total_frames / window_frames`. |
| `total_frames` | INT, `5447` | Total timeline length in frames. |
| `window_frames` | INT, `240` | Frames per block. Must match the value set on the LTX Resolution Selector. |
| `seed_base` | INT, `1000` | Base seed. Each block adds its index unless `fixed_seed` is on. |
| `fixed_seed` | BOOLEAN, `False` | When on, every block uses `seed_base` unchanged (deterministic runs). |
| `keyframe_len` | INT, `8` | Length in frames of the injected keyframe reference. |
| `img_compression` | INT, `0` | `img_compression` passed to the director. |
| `mode` | `keyframe` / `latent` / `full` | Handoff mechanism between blocks (see table below). |
| `reference_mode` | `chained` / `fixed_first` / `fixed_custom` | Which image anchors the next block (see table below). |
| `subfolder` | STRING, `whatdreamscost` | Input subfolder for keyframes and the audio file. |
| `auto_concat` | BOOLEAN, `True` | Run the final FFmpeg concat after the last block. |
| `dry_run` | BOOLEAN, `False` | Log all planned block windows and seeds without enqueueing the next block. |
| `cleanup_now` | BOOLEAN, `False` | acts as a button: deletes chain keyframes and latents instead of running the chain. |

#### Optional inputs

| Input | Type | Purpose |
| --- | --- | --- |
| `anything` | `*` | Any passthrough connection to keep the node in the graph. |
| `images` | IMAGE | The final frame tensor of the current block; the last image is saved as the lossless keyframe PNG. |
| `fixed_reference_file` | STRING | File name (relative to `input/<subfolder>/`) used when `reference_mode: fixed_custom` is selected. |
| `width_in` | INT (socket) | Resolution passthrough, e.g. from the LTX Resolution Selector. Overwrites the director's `width` for all following blocks. |
| `height_in` | INT (socket) | Resolution passthrough, e.g. from the LTX Resolution Selector. Overwrites the director's `height` for all following blocks. |

> **Note:** `window_frames` is read as a widget on the controller. Set it to the same value you chose on the LTX Resolution Selector — the controller currently has no socket input for it.

### LTX Resolution Selector

A central config node. Instead of megapixel math it offers a **direct resolution dropdown** taken from the original LTX workflow table (all entries 16:9 and divisible by 32), plus inputs for block length and frame rate.

#### Inputs

| Input | Type / Default | Purpose |
| --- | --- | --- |
| `resolution` | Dropdown, `960x544` | Direct resolution choice (see table below). |
| `window_frames` | INT, `240` | Frames per block, forwarded as an output. |
| `fps` | INT, `24` | Frame rate, forwarded as an output. |

#### Outputs

| Output | Purpose |
| --- | --- |
| `width` | Selected resolution width (e.g. `960`). Wire into the controller's `width_in`. |
| `height` | Selected resolution height (e.g. `544`). Wire into the controller's `height_in`. |
| `window_frames` | The block length, for nodes that accept it as a socket. |
| `fps` | The frame rate, for nodes that accept it as a socket. |

#### Resolution table

| Choice | Megapixels | Notes |
| --- | --- | --- |
| `608x352` | 0.2 MP | Fastest tests |
| `736x416` | 0.3 MP | |
| `864x480` | 0.4 MP | |
| `960x544` | 0.5 MP | **Default**, good speed/quality balance |
| `1056x608` | 0.6 MP | |
| `1152x640` | 0.7 MP | |
| `1216x672` | 0.8 MP | |
| `1280x736` | 0.9 MP | Closest to 720p |
| `1376x768` | 1.0 MP | |
| `1504x832` | 1.2 MP | |
| `1664x928` | 1.5 MP | |
| `1824x1024` | 1.8 MP | |
| `1920x1088` | 2.0 MP | Highest, heavy for 16 GB VRAM |

📦 Modes and Reference Strategies
---------------------------------

| Setting | Available values | Purpose | Recommended use |
| --- | --- | --- | --- |
| **Mode** | `keyframe` | Starts each subsequent block with a fresh video latent and injects an image reference. | Best choice for stable continuity and Lipsync. |
| **Mode** | `latent` | Supplies the previous block's saved latent to the next block. | Experimental; may not behave as a true temporal continuation. |
| **Mode** | `full` | Combines image-keyframe and latent handoff. | Experimental; use low HQ denoise to reduce visual drift. |
| **Reference mode** | `chained` | Uses the last frame of the immediately preceding block. | Recommended for continuous action and audio/Lipsync. |
| **Reference mode** | `fixed_first` | Uses the last frame from block 1 for later blocks. | Useful as a visual identity anchor, but can constrain facial motion. |
| **Reference mode** | `fixed_custom` | Uses a user-specified fixed image file. | Useful for controlled test shots or a deliberately static visual anchor. |

### Recommended configuration

For the most reliable result in the current workflow:

```text
mode: keyframe
reference_mode: chained
HQ denoise: 0.20–0.25
```

This keeps the next block visually grounded in the last generated frame while still allowing the audio-driven mouth and facial motion to continue naturally.

⚠️ Continuity Notes
-------------------

### Keyframe handoff

`keyframe` is the stable path for multi-block projects. The controller starts a new block with a fresh latent and supplies the preceding block's final image as a visual guide. With `reference_mode: chained`, the guide frame represents the correct moment in the ongoing video and is therefore well suited to Lipsync.

### Latent handoff

The saved latent represents the prior block rather than a small temporal history window. Passing that full latent into a later block can behave more like video-to-video reprocessing than a frame-accurate continuation. For that reason, `latent` and `full` are best treated as experimental modes in the current implementation.

### Fixed references and Lipsync

A fixed reference image can strongly preserve a person and location, but a fixed frame also contains a fixed mouth shape. If it is injected with high guide strength at each block boundary, it may conflict with the audio at that later point in time. Prefer `chained` when Lipsync matters.

🛠️ Installation
---------------

### Method 1: ComfyUI Manager

*(Once the node is registered in the Manager database)*

1. Open **ComfyUI Manager**.
2. Select **Install Custom Nodes**.
3. Search for the LTX Chain Controller.
4. Install the node and restart ComfyUI.

### Method 2: Manual installation via Git

1. Navigate to the `custom_nodes` directory of your ComfyUI installation:

   ```bash
   cd ComfyUI/custom_nodes
   ```

2. Clone the repository:

   ```bash
   git clone <REPOSITORY_URL>
   ```

3. Enter the cloned directory and install its dependencies, if the repository provides a `requirements.txt`:

   ```bash
   cd <REPOSITORY_DIRECTORY>
   pip install -r requirements.txt
   ```

4. Restart ComfyUI.

### ⚠️ Runtime requirements

* **FFmpeg** must be installed and accessible through `PATH`; it is required to concatenate the generated block videos and mux the final audio.
* Start ComfyUI with `--disable-dynamic-vram` when using this workflow with GGUF models, because dynamic VRAM handling can otherwise lead to deadlocks.
* The workflow is intended for the ComfyUI server at `http://127.0.0.1:8188` and uses the shared base directory `/mnt/seagate/ComfyUI-Shared` in the current setup.

🚀 Usage in a Workflow
----------------------

1. Add the **LTX Resolution Selector** and pick a resolution (for example `960x544`). Set `window_frames` and `fps` to your project values.
2. Wire the selector into the **LTX Chain Controller**:
   * `width` → `width_in`
   * `height` → `height_in`
   * `window_frames` / `fps` can be wired wherever a node accepts them as sockets. On the controller itself, `window_frames` is a widget — set it to the same value manually.
3. Set `total_frames` (or leave `total_blocks` at `0` for auto calculation) and configure seeds.
4. For a first stability test, choose:

   ```text
   mode: keyframe
   reference_mode: chained
   ```

5. Use a detailed `global_prompt` that locks the subject's identity, clothing, location, and visual style across the entire timeline.
6. Start the run. The controller renders the blocks sequentially, saves the required intermediate assets, and starts the final concat after all blocks finish.
7. Find the assembled output at:

   ```text
   /mnt/seagate/ComfyUI-Shared/output/final.mp4
   ```

### Resolution consistency across blocks

Block 1 renders with the director's own widgets. From block 2 onward, `width_in` / `height_in` overwrite the director's width/height, so every following block uses the selector's resolution. If block 1 was rendered at a different size, the controller prints a warning — align the director widgets with the selector so **all** blocks share one resolution.

### Prompting for identity consistency

Keep camera-shot instructions and changing actions in the local timeline segments. Put only persistent visual attributes into the global prompt: age range, hair, skin tone, facial-character description, clothing, setting, lighting, and style.

Example:

```text
Cinematic film shot, photorealistic, 35mm lens depth of field, sharp focus,
natural skin texture, masterfully lit, high professional cinematography,
consistent character features.

A young woman in her mid-20s with medium-length wavy dark brown hair, fair skin,
sharp facial features, wearing a simple modest black jacket over a black top and
blue jeans. Confident, determined expression. Neon-lit night city street, vibrant
neon signs reflecting on wet pavement, blurred colorful cityscape background, no
logos or text. Dramatic lighting, bright neon contrasted with dark shadows,
intense empowering mood. Sharp, detailed, no motion blur. Safe for work,
non-sexual, no nudity, no revealing clothing.
```

### Final concat and audio synchronisation

The individual blocks share an overlap frame at the transition, which is useful for visual handoff. During final assembly, this overlapping first frame must be removed from every block after block 1. Otherwise, the video becomes longer than the source audio by one frame per transition and audio/Lipsync gradually drifts out of sync.

The final concat therefore:

1. Keeps block 1 unchanged.
2. Trims the first overlapping frame from blocks 2 onward.
3. Concatenates the resulting video streams.
4. Muxes the project audio as the final audio stream.

The controller's internal timing and the concat assume **24 fps** — keep the selector's `fps` at `24` unless you adjust the workflow accordingly.

📁 Generated Files
------------------

| File / location | Purpose |
| --- | --- |
| `output/video/block_<n>_*.mp4` | Rendered video for each completed block. |
| `output/block_<n>_*.latent` | Saved block latent used by `latent` or `full` handoff. |
| `input/whatdreamscost/block_<n>_last.png` | Last-frame keyframe of block n (chain reference). |
| `output/keyframes/block_<n>_last.png` | Archive copy of the same keyframe. |
| `output/final.mp4` | Final concatenated video with the project audio. |

> The exact intermediate-file locations can depend on the workflow and node configuration. The paths above reflect the current shared-directory setup.

🧹 Cleanup
---------

Set `cleanup_now` to `True` and run the workflow once after cancelling a run or before starting a completely new project. This deletes all chain keyframes (`block_*_last.png`) and block latents so older artifacts cannot be reused as references in a later chain. Set it back to `False` for normal operation.

## Troubleshooting

| Symptom | Likely cause | Suggested action |
| --- | --- | --- |
| Person or location changes after a block transition | The visual anchor is too weak, the denoise value is too high, or the global prompt lacks stable identity details. | Use `keyframe` + `chained`, reduce HQ denoise to `0.20–0.25`, and describe stable identity and setting in the global prompt. |
| Lipsync fails with `fixed_first` | A static reference frame conflicts with the later audio phonemes. | Switch to `reference_mode: chained`. |
| Resolution gets lower in later blocks | The wrong latent output is being saved or reused. | Ensure the `SaveLatent` node receives the HQ-pass latent output. |
| Console warning that block 1 ran with a different width/height | Director widgets and selector disagree. | Align the director's width/height widgets with the LTX Resolution Selector so all blocks share one resolution. |
| Audio and video are out of sync in `final.mp4` | Overlap frames were concatenated without trimming. | Trim the first overlap frame from each block after block 1 during final concat (the controller does this automatically). |
| ComfyUI hangs or deadlocks under AMD / GGUF use | Dynamic VRAM management can conflict with the model-loading path. | Start ComfyUI with `--disable-dynamic-vram`. |
| Final concat is skipped | FFmpeg is not available through `PATH`. | Install FFmpeg and restart ComfyUI / the terminal session. |
| Old keyframes/latents leak into a new project | Leftover artifacts from a cancelled run. | Set `cleanup_now: True`, run once, then set it back to `False`. |

Created for the ComfyUI community. Happy rendering!
