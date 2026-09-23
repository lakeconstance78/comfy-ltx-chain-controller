# -*- coding: utf-8 -*-
"""
LTX Chain Controller v5 - Block chaining as a custom node (Windows & Linux).

Features:
  * Saves a lossless keyframe PNG per block (last frame from the IMAGE
    tensor) to input/<subfolder>/ and output/keyframes/.
  * Builds the prompt for the next block (window, timeline keyframe,
    latent handoff via CustomLoadLatent, prefixes, seed, img_compression).
  * mode controls the handoff mechanism between blocks:
      - "keyframe": image keyframe only (lossless, no latent swap,
                    most stable mode - default).
      - "latent":   latent handoff via CustomLoadLatent only (best
                    motion continuity, but more prone to
                    model-swap deadlocks with GGUF).
      - "full":     both combined (image keyframe + latent).
  * reference_mode controls which image is used as the keyframe
    reference for the next block:
      - "chained":      last frame of the respective previous block
                    (default, as before).
      - "fixed_first":  always the keyframe from block 1 (prevents
                    "drift" across many blocks).
      - "fixed_custom": always a fixed, user-defined file
                    (fixed_reference_file, path relative to
                    input/<subfolder>/), e.g. an uploaded
                    reference image.
  * Enqueues the next block with a delay from a daemon thread
    (no blocking HTTP self-call inside execution).
  * NO mm.unload_all_models() -> avoids the GGUF-Unpatch-KeyError.
    Cleanup only via /history-clear + mm.soft_empty_cache().
  * Optional final ffmpeg concat of all block videos + song -> final.mp4.
  * Optional resolution passthrough: width_in/height_in (INT sockets,
    e.g. from the LTXResolutionSelector) are written directly into the
    custom_width/custom_height widgets of the LTXDirector and apply to
    all subsequent blocks (existing connections at the Director remain
    untouched).
  * Contains the LTXResolutionSelector (resolution selection from the
    table of the original LTX workflow, all values divisible by 32) with
    outputs width/height/window_frames/fps.
"""
import json
import glob
import math
import shutil
import subprocess
import threading
import time
import urllib.request
import pathlib

import numpy as np
from PIL import Image

import folder_paths
import comfy.model_management as mm

NODE_NAME = "LTXChainController"
# Windows: full path if necessary, e.g. r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"  # installed together with ffmpeg


# ---- HTTP helpers ----
def _post(url, payload, port):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{url}", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def _newest(pattern):
    files = sorted(glob.glob(str(pattern)),
                   key=lambda p: pathlib.Path(p).stat().st_mtime)
    return pathlib.Path(files[-1]) if files else None


def _probe_video(path):
    """Reads (width, height, fps) from a video file; None if ffprobe is
    missing or the file is unreadable."""
    exe = shutil.which(FFPROBE)
    if exe is None and "ffmpeg" in FFMPEG:
        cand = FFMPEG.replace("ffmpeg", "ffprobe")
        if pathlib.Path(cand).exists():
            exe = cand
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        stream = json.loads(proc.stdout)["streams"][0]
        num, den = (stream.get("r_frame_rate") or "0/1").split("/")[:2]
        rate = float(num) / float(den) if float(den) else 0.0
        return int(stream["width"]), int(stream["height"]), rate
    except Exception:
        return None


def _resolve_expected_size(wf, d, width_in, height_in):
    """Determines the block resolution expected for the QC size check -
    even WITHOUT wired width_in/height_in inputs at the controller.

    Priority:
      1. width_in/height_in (wired directly at the controller)
      2. Chained Director inputs: if custom_width/custom_height hang on
         an LTXResolutionSelector, the chosen resolution is read
         directly from its selection.
      3. Literal values of the Director widgets ("sizing handled by the
         Director").
    Returns (width, height, source); values may be None.
    """
    exp_w = int(width_in) if width_in is not None else None
    exp_h = int(height_in) if height_in is not None else None
    if exp_w is not None and exp_h is not None:
        return exp_w, exp_h, "width_in/height_in"

    def _selector_size(link):
        # Follows a link [node_id, slot]; returns (width, height) for an
        # LTXResolutionSelector source, else None.
        if not (isinstance(link, list) and link):
            return None
        node = wf.get(str(link[0])) or wf.get(link[0])
        if not (isinstance(node, dict)
                and node.get("class_type") == "LTXResolutionSelector"):
            return None
        res = node.get("inputs", {}).get("resolution")
        if not (isinstance(res, str) and "x" in res.lower()):
            return None
        try:
            return tuple(int(p) for p in res.lower().split("x"))
        except ValueError:
            return None

    src = None
    for key in ("custom_width", "custom_height"):
        size = _selector_size(d.get(key))
        if size is None:
            continue
        if key == "custom_width":
            exp_w = size[0]
        else:
            exp_h = size[1]
        src = "Director wiring (LTXResolutionSelector)"

    lw, lh = d.get("custom_width"), d.get("custom_height")
    if exp_w is None and isinstance(lw, int) and lw > 0:
        exp_w = lw
    if exp_h is None and isinstance(lh, int) and lh > 0:
        exp_h = lh
    if exp_w is None and exp_h is None:
        return None, None, None
    return exp_w, exp_h, src or "Director widget"


def _cleanup_artifacts(out, inp, subfolder):
    """Deletes temporary chain keyframes and latent files (e.g. after an
    aborted run) so the next run starts clean."""
    patterns = [
        out / "keyframes" / "block_*_last*.png",
        inp / subfolder / "block_*_last.png",
        out / "block_*_*.latent",
    ]
    deleted = 0
    for pat in patterns:
        for f in glob.glob(str(pat)):
            try:
                pathlib.Path(f).unlink()
                deleted += 1
            except Exception as e:
                print(f"[ChainController] cleanup: could not delete {f}: {e}")
    print(f"[ChainController] Cleanup: removed {deleted} temporary file(s).")
    return deleted


# ---- Timeline surgery ----
def _clean_segments(segs):
    """Remove chain keyframes (*_last.png) and re-merge split text halves
    with identical prompts -> clean base."""
    out = [s for s in segs
           if not (s.get("type") == "image"
                   and "_last.png" in s.get("imageFile", ""))]
    merged = []
    for s in out:
        if (merged and s.get("type") == "text"
                and merged[-1].get("type") == "text"
                and merged[-1].get("prompt") == s.get("prompt")):
            last = dict(merged[-1])
            last["length"] = (s["start"] + s["length"]) - last["start"]
            merged[-1] = last
        else:
            merged.append(dict(s))
    return merged


def _build_timeline(tl_raw, s, e, keyframe_png, subfolder, kf_len):
    """Set the window, inject the chain keyframe at frame s (splitting
    text OR image segments), local_prompts/segment_lengths like the UI."""
    tl = json.loads(tl_raw)
    segs = _clean_segments(tl["segments"])
    if keyframe_png:
        img = {"id": f"chain{s}", "start": s, "length": kf_len,
               "prompt": " ", "type": "image",
               "imageFile": f"{subfolder}/{keyframe_png}",
               "imageB64": f"/api/view?filename={keyframe_png}"
                    f"&type=input&subfolder={subfolder}",
               "isEndFrame": False, "guideStrength": 1}
        for idx, seg in enumerate(segs):
            a, b = seg["start"], seg["start"] + seg["length"]
            if not (a <= s < b):
                continue
            head = dict(seg)
            head["length"] = s - a
            if seg.get("type") == "image":
                tail = {"id": seg["id"] + "t", "start": s + kf_len,
                    "length": b - (s + kf_len),
                    "prompt": seg.get("prompt", ""), "type": "text",
                    "isEndFrame": False}
            else:
                tail = dict(seg)
                tail["id"] = seg["id"] + "t"
                tail["start"] = s + kf_len
                tail["length"] = b - (s + kf_len)
            neu = ([head] if head["length"] > 0.5 else []) + [img] + \
                  ([tail] if tail["length"] > 0.5 else [])
            segs[idx:idx + 1] = neu
            break
    tl["segments"] = segs
    tl["normalStartFrame"] = s
    tl["normalDurationFrames"] = e - s
    prompts, lengths = [], []
    for seg in segs:
        a, b = seg["start"], seg["start"] + seg["length"]
        lo, hi = max(a, s), min(b, e)
        if hi - lo > 0.01:
            prompts.append(seg.get("prompt", "") or "")
            lengths.append(str(hi - lo))
    return json.dumps(tl), " | ".join(prompts), ",".join(lengths)


# ---- Resolution selector (as in the original LTX workflow) ----
# Table from the original workflow (16:9, all values divisible by 32)
LTX_RESOLUTION_TABLE = {
    "608x352":   (608, 352),    # 0.2 MP
    "736x416":   (736, 416),    # 0.3 MP
    "864x480":   (864, 480),    # 0.4 MP
    "960x544":   (960, 544),    # 0.5 MP
    "1056x608":  (1056, 608),   # 0.6 MP
    "1152x640":  (1152, 640),   # 0.7 MP
    "1216x672":  (1216, 672),   # 0.8 MP
    "1280x736":  (1280, 736),   # 0.9 MP (~720p)
    "1280x768":  (1280, 768),   # 0.98 MP, Director-compatible 16:9 bucket
    "1376x768":  (1376, 768),   # 1.0 MP
    "1504x832":  (1504, 832),   # 1.2 MP
    "1664x928":  (1664, 928),   # 1.5 MP
    "1824x1024": (1824, 1024),  # 1.8 MP
    "1920x1088": (1920, 1088),  # 2.0 MP
}

LTX_RESOLUTION_CHOICES = [f"{w}x{h}" for w, h in LTX_RESOLUTION_TABLE.values()]


class LTXResolutionSelector:
    """Central config node: resolution selection as in the original LTX
    workflow (the aspect ratio follows directly from the chosen
    resolution, all entries are 16:9). Also provides inputs for
    window_frames and fps, which are passed through as outputs of the
    same name (e.g. to the LTX Chain Controller)."""
    CATEGORY = "LTX/Chain"
    FUNCTION = "select"
    RETURN_TYPES = ("INT", "INT", "INT", "INT")
    RETURN_NAMES = ("width", "height", "window_frames", "fps")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "resolution": (LTX_RESOLUTION_CHOICES,
                    {"default": "960x544"}),
                "window_frames": ("INT", {"default": 240, "min": 8,
                    "max": 9999}),
                "fps": ("INT", {"default": 24, "min": 1, "max": 120}),
            }
        }

    def select(self, resolution, window_frames, fps):
        width, height = LTX_RESOLUTION_TABLE[resolution]
        return (int(width), int(height), int(window_frames), int(fps))


# ---- The node ----
class LTXChainController:
    """Automatically runs the next block after each block."""
    CATEGORY = "LTX/Chain"
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = ()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "block_index":     ("INT",   {"default": 0, "min": 0, "max": 9999}),
                "total_blocks":    ("INT",   {"default": 0, "min": 0, "max": 9999}),
                "total_frames":    ("INT",   {"default": 5447, "min": 1, "max": 9999}),
                "window_frames":   ("INT",   {"default": 240, "min": 8, "max": 9999}),
                "seed_base":       ("INT",   {"default": 1000, "max": 9999}),
                "fixed_seed":      ("BOOLEAN", {"default": False}),
                "keyframe_len":    ("INT",   {"default": 8, "min": 1, "max": 999}),
                "img_compression": ("INT",   {"default": 0, "min": 0, "max": 51}),
                "mode":            (["keyframe", "latent", "full"], {"default": "keyframe"}),
                "reference_mode":  (["chained", "fixed_first", "fixed_custom"],
                    {"default": "chained"}),
                "subfolder":       ("STRING", {"default": "whatdreamscost"}),
                "auto_concat":     ("BOOLEAN", {"default": True}),
                "dry_run":         ("BOOLEAN", {"default": False}),
                "cleanup_now":     ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "anything": ("*",),
                "images":   ("IMAGE",),
                "fixed_reference_file": ("STRING", {"default": ""}),
                "width_in":  ("INT", {"forceInput": True}),
                "height_in": ("INT", {"forceInput": True}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    # ---- Prefix helper ----
    @staticmethod
    def _set_prefix(wf, class_type, value):
        for v in wf.values():
            if v.get("class_type") != class_type:
                continue
            inp = v["inputs"].get("filename_prefix")
            if isinstance(inp, list):
                src = wf[inp[0]]["inputs"]
                if "value" in src:
                    src["value"] = value.split("/")[-1]
                elif "string_b" in src:
                    src["string_b"] = value.split("/")[-1]
                else:
                    v["inputs"]["filename_prefix"] = value
            else:
                v["inputs"]["filename_prefix"] = value

    # ---- Deferred enqueue ----
    def _deferred_next_block(self, wf, port, delay=20.0):
        """Enqueues the next block from a daemon thread."""
        def _job():
            time.sleep(delay)
            try:
                _post("/prompt", {"prompt": wf}, port)
                print("[ChainController] Next block enqueued.")
            except Exception as e:
                print(f"[ChainController] Enqueue failed: {e}")
        threading.Thread(target=_job, daemon=True).start()

    # ---- Final concat ----
    @staticmethod
    def _concat(out, inp, subfolder, prompt, total_blocks,
                target_width=None, target_height=None):
        vids = []
        for b in range(1, total_blocks + 1):
            cands = sorted(
                glob.glob(str(out / "video" / f"block_{b}_*.mp4")),
                key=lambda p: pathlib.Path(p).stat().st_mtime,
            )
            if not cands:
                print(f"[ChainController] WARNING: no video found for block {b}")
                continue
            vf = pathlib.Path(cands[-1])
            if vf.stat().st_size == 0:
                print(f"[ChainController] WARNING: {vf.name} is 0 bytes and "
                      f"will be skipped (probably a leftover from an "
                      f"aborted run).")
                continue
            vids.append(vf)
            print(f"[ChainController]   concat input: {vf.name} "
                  f"({vf.stat().st_size / 1024 / 1024:.1f} MB)")

        if not vids:
            print("[ChainController] No block videos found for concat.")
            return

        # Log the actual resolution/frame rate of every block - deviations
        # between the blocks are the most common cause of concat failures.
        dims = [_probe_video(v) for v in vids]
        ref = next((d for d in dims if d), None)
        # Use the frame rate actually measured in the block videos instead of
        # a hardcoded 24 - a wrong fps silently shifts every trim and
        # desynchronizes audio and video more with each block boundary.
        fps = ref[2] if (ref and ref[2] and 8 <= ref[2] <= 120) else 24.0
        normalize = False
        target = None
        if target_width is not None and target_height is not None:
            target = (int(target_width), int(target_height))
        elif ref:
            target = (ref[0], ref[1])

        if ref:
            for vf, d_ in zip(vids, dims):
                wh = f"{d_[0]}x{d_[1]}" if d_ else "unknown"
                rate = f" @ {d_[2]:.2f}fps" if (d_ and d_[2]) else ""
                print(f"[ChainController]   concat size: {vf.name} -> {wh}{rate}")
            normalize = any(
                d_ and target and (d_[0], d_[1]) != target for d_ in dims
            )
            if normalize and target:
                print(f"[ChainController] WARNING: block videos will be "
                      f"scaled/padded uniformly to {target[0]}x{target[1]} "
                      f"for the final concat.")

        song = ""
        try:
            d = next(
                v["inputs"]
                for v in prompt.values()
                if v.get("class_type") == "LTXDirector"
            )
            song = json.loads(d["timeline_data"])["audioSegments"][0]["fileName"]
        except Exception:
            pass

        has_song = bool(song) and (inp / subfolder / song).exists()
        if song and not has_song:
            print(f"[ChainController] WARNING: song '{song}' not found - "
                  f"concat will run without audio.")

        if shutil.which(FFMPEG) is None:
            print("[ChainController] ffmpeg not found on PATH - concat skipped.")
            return

        final = out / "final.mp4"

        def _run_ffmpeg(cmd, label):
            """Runs ffmpeg and shows the real messages on failure - the
            old check=False swallowed errors and still printed
            'final.mp4 written'."""
            print(f"[ChainController] ffmpeg {label} ...")
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                tail = "\n".join(lines[-15:]) if lines else "(no output)"
                print(f"[ChainController] ffmpeg {label} FAILED "
                      f"(exit {proc.returncode}):\n{tail}")
            return proc

        inputs = []
        filter_parts = []

        for i, vf in enumerate(vids):
            inputs += ["-i", str(vf)]

            # Keep block 1 entirely. From block 2 on, remove the overlapping
            # first frame - frame-exact via start_frame=1. The old time-based
            # trim=start=1/fps depended on float rounding and the input
            # timebase and could drop one frame too many or too few per
            # boundary, which desynchronizes the video from the song
            # cumulatively.
            chain = []
            if i > 0:
                chain.append("trim=start_frame=1")
            chain.append("setpts=PTS-STARTPTS")
            if normalize:
                # Normalize mixed resolutions to the size of block 1,
                # otherwise the concat filter refuses to work.
                chain += [
                    f"scale={target[0]}:{target[1]}:"
                    f"force_original_aspect_ratio=decrease",
                    f"pad={target[0]}:{target[1]}:(ow-iw)/2:(oh-ih)/2",
                ]
            # Re-stamp every block onto a clean constant-frame-rate grid
            # with a common time base. This heals VFR jitter from the
            # encoder so the drift cannot accumulate across boundaries.
            chain.append(f"fps={fps:g}")
            chain.append("settb=AVTB")
            filter_parts.append(
                f"[{i}:v]" + ",".join(chain) + f"[v{i}]"
            )

        concat_inputs = "".join(f"[v{i}]" for i in range(len(vids)))
        filter_parts.append(
            f"{concat_inputs}concat=n={len(vids)}:v=1:a=0[vout]"
        )

        cmd = [FFMPEG, "-y", "-loglevel", "error", *inputs]

        if has_song:
            cmd += ["-i", str(inp / subfolder / song)]

        cmd += [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[vout]",
        ]

        if has_song:
            cmd += [
                "-map", f"{len(vids)}:a:0",
                "-c:a", "aac",
                "-shortest",
            ]

        cmd += [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(final),
        ]

        proc = _run_ffmpeg(cmd, "filter concat")

        # Failed or 0 bytes? -> remove the empty file, try the fallback.
        # With mixed resolutions the demuxer fallback does not help
        # (it cannot normalize) - only the filter graph can do that.
        ok = proc.returncode == 0 and final.exists() \
            and final.stat().st_size > 0
        if not ok and not normalize:
            if final.exists() and final.stat().st_size == 0:
                final.unlink()
            print("[ChainController] Fallback: concat demuxer instead of "
                  "filter graph (overlap frame removed via inpoint) ...")
            lst = out / "concat_list.txt"
            with open(lst, "w") as f:
                for i, vf in enumerate(vids):
                    f.write(f"file '{vf}'\n")
                    if i > 0:
                        f.write(f"inpoint {1 / fps:.8f}\n")
            cmd2 = [FFMPEG, "-y", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", str(lst)]
            if has_song:
                cmd2 += ["-i", str(inp / subfolder / song)]
            cmd2 += ["-map", "0:v:0", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
            if has_song:
                cmd2 += ["-map", "1:a:0", "-c:a", "aac", "-shortest"]
            cmd2 += ["-movflags", "+faststart", str(final)]
            proc = _run_ffmpeg(cmd2, "concat demuxer")
            if proc.returncode == 0:
                lst.unlink()
        elif not ok and normalize:
            print("[ChainController] Demuxer fallback skipped: with mixed "
                  "resolutions only the filter graph can normalize - "
                  "see the warning above.")

        if final.exists() and final.stat().st_size > 0:
            print(f"[ChainController] final.mp4 OK: "
                  f"{final.stat().st_size / 1024 / 1024:.1f} MB -> {final}")
        else:
            if final.exists():
                final.unlink()
            print("[ChainController] ERROR: final.mp4 could not be created "
                  "- check the ffmpeg error message above.")

    # ---- Main logic ----
    def run(self, block_index, total_blocks, total_frames, window_frames,
            seed_base, fixed_seed, keyframe_len, img_compression, mode,
            reference_mode, subfolder,
            auto_concat, dry_run, cleanup_now, anything=None, images=None,
            fixed_reference_file="", width_in=None, height_in=None,
            prompt=None):
        out = pathlib.Path(folder_paths.get_output_directory())
        inp = pathlib.Path(folder_paths.get_input_directory())

        # ---- "Button": cleanup instead of a normal chain run ----
        if cleanup_now:
            _cleanup_artifacts(out, inp, subfolder)
            return ()

        if total_blocks == 0:
            total_blocks = math.ceil(total_frames / window_frames)
            print(f"[ChainController] Auto: {total_blocks} blocks from "
                  f"{total_frames} frames @ {window_frames} frames/block")

        b = block_index + 1  # number of the block that just finished

        # Determine the resolution expected for the QC size check. This
        # also works WITHOUT wired width_in/height_in: chained Director
        # inputs (e.g. selector -> custom_width/custom_height) and
        # Director widgets are evaluated as well.
        d0 = next((v["inputs"] for v in prompt.values()
                   if v.get("class_type") == "LTXDirector"), {})
        exp_w, exp_h, size_src = _resolve_expected_size(
            prompt, d0, width_in, height_in)

        # 1) Save a lossless keyframe DIRECTLY from the tensor
        key_dst = None
        if images is not None:
            arr = (images[-1].cpu().numpy() * 255.0).round().clip(0, 255).astype(np.uint8)
            key_dst = inp / subfolder / f"block_{b}_last.png"
            key_dst.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(arr).save(key_dst)
            arc = out / "keyframes" / f"block_{b}_last.png"
            arc.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(key_dst, arc)
            print(f"[ChainController] Block {b}: keyframe saved ({key_dst.name})")

            # Early warning: did this block render at a resolution other
            # than expected? (IMAGE tensor: [B, H, W, C]) The expectation
            # comes from width_in/height_in, the Director wiring
            # (selector) or the Director widgets.
            if exp_w is not None or exp_h is not None:
                h_img = int(images[-1].shape[0])
                w_img = int(images[-1].shape[1])
                diffs = []
                if exp_w is not None and int(exp_w) != w_img:
                    diffs.append(f"width: expected {exp_w}, "
                                 f"rendered {w_img}")
                if exp_h is not None and int(exp_h) != h_img:
                    diffs.append(f"height: expected {exp_h}, "
                                 f"rendered {h_img}")
                if diffs:
                    print(f"[ChainController] WARNING: Block {b} rendered "
                          f"at {w_img}x{h_img} ({'; '.join(diffs)}) - "
                          f"align the resolution source ({size_src}), "
                          f"otherwise the blocks run at different sizes.")

        # 2) Find the block latent + QC
        prev_latent = _newest(out / f"block_{b}_*.latent")
        if prev_latent is not None and key_dst is not None:
            print(f"[ChainController]   QC: keyframe "
                  f"{key_dst.stat().st_size / 1024:7.1f} KB | latent "
                  f"{prev_latent.stat().st_size / 1024:7.1f} KB")

        ni = block_index + 1  # 0-based index of the NEXT block
        if ni >= total_blocks:
            print(f"[ChainController] All {total_blocks} blocks finished.")
            if auto_concat:
                self._concat(out, inp, subfolder, prompt, total_blocks)
            return ()

        # 3) Build the prompt for the next block
        wf = json.loads(json.dumps(prompt))
        my_id = next(k for k, v in wf.items() if v.get("class_type") == NODE_NAME)
        wf[my_id]["inputs"]["block_index"] = ni

        d = next(v["inputs"] for v in wf.values()
                 if v.get("class_type") == "LTXDirector")

        # Resolution passthrough (e.g. from the LTXResolutionSelector):
        # the values are written directly into the custom_width/
        # custom_height widgets of the Director and thus apply to all
        # subsequent blocks. If a connection already exists at the
        # Director (as when using the selector), it is left untouched -
        # then the selector delivers the same value for every block.
        if width_in is not None or height_in is not None:
            for _k, _v in (("custom_width", width_in),
                           ("custom_height", height_in)):
                if _v is None:
                    continue
                _cur = d.get(_k)
                if isinstance(_cur, list):
                    # Link reference (e.g. selector) -> the value comes
                    # from the graph per block, do not overwrite here.
                    continue
                if _cur is None:
                    print(f"[ChainController] WARNING: LTXDirector has no "
                          f"'{_k}' widget - {_k} passthrough skipped.")
                    continue
                if int(_cur) != int(_v):
                    d[_k] = int(_v)
                    if block_index == 0:
                        print(f"[ChainController] Block 1 ran with {_k}="
                              f"{_cur}, selector delivers {_v} - "
                              f"subsequent blocks run with {_v}.")
        elif block_index == 0:
            # Sizing deliberately runs via the Director (widgets or
            # selector wiring at the Director) - this is a fully valid
            # mode, not an error. Just announce what the QC checks against.
            if exp_w is not None or exp_h is not None:
                print(f"[ChainController] Resolution QC active: expected "
                      f"{exp_w}x{exp_h} (source: {size_src}) - "
                      f"width_in/height_in at the controller are unconnected.")
            else:
                print("[ChainController] NOTE: resolution not detectable "
                      "(width_in/height_in unconnected, Director without "
                      "evaluable size info) - QC without size check.")

        # The overlap (1 frame) accumulates per block
        overlap = ni
        s = (ni * window_frames) - overlap
        e = min(s + window_frames, total_frames)

        use_kf = mode in ("keyframe", "full") and ni > 0
        use_lat = mode in ("latent", "full") and ni > 0
        key_png = None
        if use_kf:
            if reference_mode == "fixed_first":
                key_png = "block_1_last.png"
            elif reference_mode == "fixed_custom":
                if fixed_reference_file.strip():
                    key_png = fixed_reference_file.strip()
                else:
                    print("[ChainController] WARNING: reference_mode="
                          "'fixed_custom' but fixed_reference_file is "
                          "empty - falling back to 'chained'.")
                    key_png = f"block_{b}_last.png"
            else:  # "chained" (default)
                key_png = f"block_{b}_last.png"

        tl_str, loc, segl = _build_timeline(d["timeline_data"], s, e,
                    key_png, subfolder, keyframe_len)
        d.update(start_frame=s, end_frame=e, duration_frames=e - s,
                 start_second=round(s / 24, 3), end_second=round(e / 24, 3),
                 duration_seconds=round((e - s) / 24, 3),
                 timeline_data=tl_str, local_prompts=loc, segment_lengths=segl,
                 guide_strength="1.00", img_compression=img_compression)

        current_seed = seed_base if fixed_seed else (seed_base + ni)
        self._set_prefix(wf, "SaveLatent", f"block_{ni + 1}")
        self._set_prefix(wf, "SaveVideo", f"video/block_{ni + 1}")
        for v in wf.values():
            if v.get("class_type") == "RandomNoise":
                v["inputs"]["noise_seed"] = current_seed
            if v.get("class_type") == "CreateVideo":
                v["inputs"].pop("audio", None)  # avoid the AAC/NaN crash

        # Latent handoff (only for mode "latent" or "full").
        # IMPORTANT: never delete nodes from the workflow that this node
        # did not inject itself. The previously hard-wired ID "300" could
        # collide with real user nodes (e.g. a node connected to the
        # LTXDirector): the pop left a dangling link, and the prompt
        # validation of the enqueued block aborted with
        # "Exception when validating inner node: '300'".
        if use_lat and prev_latent is not None:
            # Detect an already-injected chain latent node by its
            # signature and reuse it (collision-free ID).
            latent_id = next((k for k, v_ in wf.items()
                              if isinstance(v_, dict)
                              and v_.get("class_type") == "CustomLoadLatent"
                              and isinstance(v_.get("_meta"), dict)
                              and v_["_meta"].get("title") == "Chain Latent Load"),
                             None)
            if latent_id is None:
                latent_id = str(max((int(k) for k in wf
                                     if str(k).isdigit()), default=0) + 1)
            wf[latent_id] = {"class_type": "CustomLoadLatent",
                             "inputs": {"file_path": str(prev_latent)},
                             "_meta": {"title": "Chain Latent Load"}}
            d["optional_latent"] = [latent_id, 0]
        else:
            if use_lat:
                print(f"[ChainController] WARNING: mode='{mode}' but no "
                      f"latent found for block {b} - latent handoff is "
                      f"skipped for this block.")
            # Only detach the link at the Director - referenced nodes
            # (e.g. the user's own nodes) remain untouched.
            if isinstance(d.get("optional_latent"), list):
                print(f"[ChainController] optional_latent link at the "
                      f"Director removed (mode='{mode}', no latent "
                      f"handoff).")
            d.pop("optional_latent", None)

        print(f"[ChainController] Queue block {ni + 1}/{total_blocks} | "
              f"frames {s}-{e} | seed {current_seed} "
              f"({'fixed' if fixed_seed else 'dynamic'}) | mode={mode} | "
              f"keyframe {key_png or '-'} | "
              f"latent {prev_latent.name if (use_lat and prev_latent) else '-'}")
        if dry_run:
            return ()

        port = 8188
        try:
            from server import PromptServer
            port = PromptServer.instance.port
        except Exception:
            pass
        self._deferred_next_block(wf, port)
        return ()


NODE_CLASS_MAPPINGS = {
    NODE_NAME: LTXChainController,
    "LTXResolutionSelector": LTXResolutionSelector,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_NAME: "LTX Chain Controller",
    "LTXResolutionSelector": "LTX Resolution Selector",
}