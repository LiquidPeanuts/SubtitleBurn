import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import json
import queue
import threading
import random
from concurrent.futures import ThreadPoolExecutor

# -----------------------
# Config / Helpers
# -----------------------
# Group encoders by codec type
codec_groups = {
    "av1": ["av1_nvenc", "av1_amf", "av1_qsv"],
    "hevc": ["hevc_nvenc", "hevc_amf", "hevc_qsv"],
    "h264": ["h264_nvenc", "h264_amf", "h264_qsv"]
}

def format_size(num_bytes):
    """Return human-readable file size string (B, KB, MB, GB)."""
    try:
        num_bytes = float(num_bytes)
    except Exception:
        num_bytes = 0.0
    if num_bytes < 1024:
        return f"{num_bytes:.0f} B"
    elif num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024**3:
        return f"{num_bytes / 1024**2:.1f} MB"
    else:
        return f"{num_bytes / 1024**3:.2f} GB"

# -----------------------
# Encoder detection (unchanged)
# -----------------------
def test_encoder(encoder_name):
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
        "-f", "lavfi", "-i", "testsrc2=duration=0.2:size=320x240:rate=1",
        "-c:v", encoder_name,
        "-c:a", "aac",
        "-f", "null", "-"
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=4)
        return encoder_name, True
    except subprocess.SubprocessError:
        return encoder_name, False

def test_group(encoders):
    results = {}
    for enc in encoders:
        name, available = test_encoder(enc)
        results[name] = available
        print(f"{name}: {'AVAILABLE' if available else 'UNAVAILABLE'}")
    return results

def getallmyencoders():
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(test_group, group) for group in codec_groups.values()]
        final_results = {}
        for future in futures:
            final_results.update(future.result())

    print("\n=== FINAL RESULTS ===")
    for enc, status in final_results.items():
        print(f"{enc}: {'Available' if status else 'None'}")
    true_encodings = [enc for enc, status in final_results.items() if status]
    return true_encodings

def get_available_encoders():
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    return result.stdout

def pick_best_encoder(encoders):
    # encoders can be list or string; check with 'in'
    if "hevc_nvenc" in encoders:
        best_hevc = "hevc_nvenc"
    elif "hevc_qsv" in encoders:
        best_hevc = "hevc_qsv"
    elif "hevc_amf" in encoders:
        best_hevc = "hevc_amf"
    else:
        best_hevc = "libx265"

    if "h264_nvenc" in encoders:
        best264 = "h264_nvenc"
    elif "h264_qsv" in encoders:
        best264 = "h264_qsv"
    elif "h264_amf" in encoders:
        best264 = "h264_amf"
    else:
        best264 = "libx264"

    if "av1_nvenc" in encoders:
        bestav1 = "av1_nvenc"
    elif "av1_qsv" in encoders:
        bestav1 = "av1_qsv"
    elif "av1_amf" in encoders:
        bestav1 = "av1_amf"
    else:
        bestav1 = "libxav1"

    print([bestav1, best_hevc, best264])
    return [bestav1, best_hevc, best264]

# -----------------------
# Stream probing (unchanged)
# -----------------------
def get_streams(file_path):
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-show_streams", "-of", "json",
        file_path
    ]
    result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = json.loads(result.stdout)
    audio_choices = []
    subtitle_choices = []

    for stream in data["streams"]:
        if stream["codec_type"] == "audio":
            idx = stream["index"]
            lang = stream.get("tags", {}).get("language", "und")
            desc = f"{stream['codec_name']} ({lang}):{idx}"
            audio_choices.append(desc)
        elif stream["codec_type"] == "subtitle":
            idx = stream["index"]
            lang = stream.get("tags", {}).get("language", "und")
            desc = f"{stream['codec_name']} ({lang}):{idx}"
            subtitle_choices.append(desc)
    return subtitle_choices, audio_choices

# -----------------------
# Scanning folders: now includes video count + size
# -----------------------
def scan_folders(root_folder):
    folders = {}
    for dirpath, dirnames, files in os.walk(root_folder):
        # Consider a set of typical video extensions
        video_exts = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm")
        direct_videos = [f for f in files if f.lower().endswith(video_exts)]
        if not direct_videos:
            continue

        state_file = os.path.join(dirpath, "burn_state.json")
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                state = json.load(f)
            done = state.get("finished", [])
            pending = state.get("pending", [])
            # If pending empty -> recalc
            if not pending:
                pending = [f for f in direct_videos if f not in done]
        else:
            done = []
            pending = direct_videos.copy()
            state = {"finished": [], "pending": pending}
        # Save initial or updated state
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

        log(f"Scanning {dirpath}, found videos: {direct_videos}")
        # Probe the first video for streams
        probe_video = os.path.join(dirpath, direct_videos[0]) if direct_videos else None
        if probe_video:
            subs, audios = get_streams(probe_video)
        else:
            subs, audios = [], []

        # compute total size
        total_size = 0
        for vf in direct_videos:
            try:
                total_size += os.path.getsize(os.path.join(dirpath, vf))
            except Exception:
                pass

        folders[dirpath] = {
            "files": pending,               # files we're tracking to burn (video filenames)
            "done": done,
            "subs": [f"{s}" for s in subs],
            "audios": [f"{a}" for a in audios],
            "selected_sub": tk.StringVar(),
            "selected_audio": tk.StringVar(),
            "video_count": len(direct_videos),
            "video_size": total_size,
            # ui handles:
            "ui_info_label": None,
            "ui_progress": None,
        }

        # Auto-select subtitle (prefer Japanese)
        if folders[dirpath]["subs"]:
            subs = folders[dirpath]["subs"]
            jpn_subs = [s for s in subs if "(jpn)" in s.lower() or "jpn" in s.lower() or "japanese" in s.lower()]
            if jpn_subs:
                folders[dirpath]["selected_sub"].set(jpn_subs[0])
            else:
                folders[dirpath]["selected_sub"].set(subs[0])

        # Auto-select audio (prefer Japanese)
        if folders[dirpath]["audios"]:
            audios = folders[dirpath]["audios"]
            jpn_candidates = [a for a in audios if "(jpn)" in a.lower() or "jpn" in a.lower() or "japanese" in a.lower()]
            if jpn_candidates:
                folders[dirpath]["selected_audio"].set(jpn_candidates[0])
            else:
                folders[dirpath]["selected_audio"].set(audios[0])

    return folders

# -----------------------
# State helper
# -----------------------
def mark_done(folder, f):
    state_file = os.path.join(folder, "burn_state.json")
    if os.path.exists(state_file):
        with open(state_file, "r") as sf:
            state = json.load(sf)
    else:
        state = {"finished": [], "pending": []}

    if f not in state["finished"]:
        state["finished"].append(f)
    if f in state["pending"]:
        state["pending"].remove(f)

    with open(state_file, "w") as sf:
        json.dump(state, sf, indent=2)

# -----------------------
# Utilities for thread-safe UI updates
# -----------------------
def ui_set_progress(folder_path, percent):
    """Set per-folder progress (0-100) from any thread."""
    def _set():
        info = folders.get(folder_path)
        if info and info.get("ui_progress"):
            info["ui_progress"]["value"] = percent
    root.after(0, _set)

def ui_set_global_progress(percent):
    def _set():
        global_progress["value"] = percent
    root.after(0, _set)

def ui_append_log(text):
    def _append():
        log_text.insert(tk.END, text + "\n")
        log_text.see(tk.END)
    root.after(0, _append)

def log(message):
    # route through ui_append_log for thread-safe updates
    ui_append_log(message)

# -----------------------
# Preview helper
# -----------------------
def preview_video(folder, data):
    """Preview the first video in the folder using selected tracks."""
    if not data["files"]:
        messagebox.showerror("Error", f"No video files found in {folder}")
        return

    first_file = os.path.join(folder, data["files"][0])
    # Safely extract indices
    try:
        sub_idx = data["selected_sub"].get().split(":")[1]
    except Exception:
        sub_idx = None
    try:
        aud_idx = data["selected_audio"].get().split(":")[1]
    except Exception:
        aud_idx = None

    # Build ffplay command. Use map for audio; subtitles via vf filter
    cmd = ["ffplay", "-hide_banner", "-loglevel", "error", "-i", first_file]
    if aud_idx is not None and aud_idx != "":
        # ffplay will select the mapped audio track
        cmd += ["-map", f"0:v:0", "-map", f"0:{aud_idx}"]
    if sub_idx is not None and sub_idx != "":
        # subtitles filter referencing the same file with stream index
        # quoting is important on Windows; subprocess handles it safely when passed as a list
        vf = f"subtitles={first_file}:si={sub_idx}"
        cmd += ["-vf", vf]

    try:
        subprocess.Popen(cmd)
    except FileNotFoundError:
        messagebox.showerror("Error", "ffplay not found. Please ensure FFmpeg is installed and ffplay is in PATH.")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to launch preview: {e}")

# -----------------------
# UI: select folder and build folder widgets
# -----------------------
def select_folder():
    global folders, total_tasks, completed_tasks
    parent = filedialog.askdirectory()
    if not parent:
        return

    # Clear previous UI entries
    for widget in scrollable_frame.winfo_children():
        widget.destroy()

    folders.clear()
    folders.update(scan_folders(parent))

    # Build UI rows for each folder that has files
    row = 0
    for folder_path, data in folders.items():
        if not data["files"]:
            continue

        # Folder title (wrapped)
        lbl = ttk.Label(scrollable_frame, text=f"Folder: {folder_path}", wraplength=650, justify="left")
        lbl.grid(row=row, column=0, sticky="w", columnspan=3, pady=(8, 0))
        row += 1

        # Info label: count + size
        info = ttk.Label(scrollable_frame, text=f"🎞 {data['video_count']} videos — {format_size(data['video_size'])}", font=("Segoe UI", 9, "italic"))
        info.grid(row=row, column=0, sticky="w", columnspan=3)
        folders[folder_path]["ui_info_label"] = info
        row += 1

        # Subtitle dropdown
        ttk.Label(scrollable_frame, text="Subtitle:").grid(row=row, column=0, sticky="e")
        if data["subs"]:
            sub_widget = ttk.OptionMenu(scrollable_frame, data["selected_sub"], data["selected_sub"].get(), *data["subs"])
        else:
            data["selected_sub"].set("")
            sub_widget = ttk.Label(scrollable_frame, text="(none)")
        sub_widget.grid(row=row, column=1, sticky="w")
        row += 1

        # Audio dropdown
        ttk.Label(scrollable_frame, text="Audio:").grid(row=row, column=0, sticky="e")
        if data["audios"]:
            aud_widget = ttk.OptionMenu(scrollable_frame, data["selected_audio"], data["selected_audio"].get(), *data["audios"])
        else:
            data["selected_audio"].set("")
            aud_widget = ttk.Label(scrollable_frame, text="(none)")
        aud_widget.grid(row=row, column=1, sticky="w")
        row += 1

        # Preview button
        preview_btn = ttk.Button(scrollable_frame, text="Preview", command=lambda f=folder_path, d=data: preview_video(f, d))
        preview_btn.grid(row=row, column=0, pady=(4,0), padx=(0,8), sticky="w")

        # Spacer/advice label (optional)
        hint = ttk.Label(scrollable_frame, text="(Preview uses ffplay - ensure it's installed)")
        hint.grid(row=row, column=1, sticky="w")
        row += 1

        # Folder progress bar
        prog = ttk.Progressbar(scrollable_frame, orient="horizontal", length=500, mode="determinate")
        prog.grid(row=row, column=0, columnspan=3, sticky="we", pady=(4, 8))
        folders[folder_path]["ui_progress"] = prog
        folders[folder_path]["ui_progress_value"] = 0
        row += 1

    # After building UI, compute total tasks (number of files to burn)
    total_tasks = sum(len(d["files"]) for d in folders.values())
    completed_tasks = 0
    ui_set_global_progress(0)

# -----------------------
# Detect 10-bit (duplicate kept for robustness)
# -----------------------
def is_10bit(input_path):
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt",
        "-of", "default=nokey=1:noprint_wrappers=1",
        input_path
    ]
    result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pix_fmt = result.stdout.strip()
    return "10" in pix_fmt

# -----------------------
# Main run logic (uses your ffmpeg burning flow) with progress updates
# -----------------------
def run():
    global folders, total_tasks, completed_tasks
    # Choose encoders
    try:
        enc_list = getallmyencoders()
    except Exception:
        enc_list = get_available_encoders()
    bestav1, best265, best264 = pick_best_encoder(enc_list)

    if not folders:
        messagebox.showerror("Error", "No folders selected. Please select a folder first.")
        return

    # Compute total tasks again in case
    total_tasks = sum(len(data["files"]) for data in folders.values())
    completed_tasks = 0

    # Process each folder
    for folder, data in folders.items():
        if not data["files"]:
            continue

        outfolder = folder + "-burned"
        os.makedirs(outfolder, exist_ok=True)

        # We'll operate relative to folder for extracting subs etc.
        prev_cwd = os.getcwd()
        try:
            os.chdir(folder)
        except Exception:
            pass

        num_files = len(data["files"])
        processed_in_folder = 0

        for f in list(data["files"]):  # copy to avoid concurrent modification
            input_f = os.path.join(folder, f)
            output_f = os.path.join(outfolder, f"{os.path.splitext(f)[0]}-burned.mp4")
            subname = f"subs{random.random()}.ass"
            subtitle_file = os.path.join(folder, subname)

            # Attempt to get indices; guard against malformed strings
            try:
                sub_idx = data["selected_sub"].get().split(":")[1]
            except Exception:
                # if no subtitle selected, skip extracting/burning subtitles but still copy audio/video
                sub_idx = None
            try:
                aud_idx = data["selected_audio"].get().split(":")[1]
            except Exception:
                aud_idx = None

            # choose encoder and vf_option based on bit depth
            try:
                input_is_10bit = is_10bit(input_f)
            except Exception:
                input_is_10bit = False

            if input_is_10bit:
                log(f"Detected 10-bit input for: {f}")
                chosen_encoder = best265
                vf_option = rf"subtitles={subname}"  # preserve bit depth
            else:
                log(f"Detected 8-bit input for: {f}")
                chosen_encoder = best264
                vf_option = rf"format=yuv420p,subtitles={subname}"  # force 8-bit

            # If we have a subtitle index, extract it; otherwise skip extracting and don't set subtitles VF
            if sub_idx is not None and sub_idx != "":
                extract_cmd = [
                    "ffmpeg", "-y", "-i", input_f, "-map", f"0:{sub_idx}", subtitle_file
                ]
                try:
                    result = subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    log(result.stdout)
                except Exception as e:
                    log(f"Failed to extract subtitle for {f}: {e}")
                    try:
                        if os.path.exists(subtitle_file):
                            os.remove(subtitle_file)
                    except Exception:
                        pass
                    sub_idx = None
                    vf_option = ""
            else:
                vf_option = ""  # no subtitle filter if none

            # Build burn command
            burn_cmd = [
                "ffmpeg", "-y", "-hwaccel", "auto",
                "-i", input_f,
            ]
            # Map video (first video)
            burn_cmd += ["-map", f"0:v:0"]
            # Map chosen audio if exists else copy any audio
            if aud_idx is not None and aud_idx != "":
                burn_cmd += ["-map", f"0:{aud_idx}"]
            else:
                burn_cmd += ["-map", "0:a?"]  # optional any audio

            # Add video filters if specified
            if vf_option:
                burn_cmd += ["-vf", rf"{vf_option}"]

            burn_cmd += ["-c:v", chosen_encoder, "-c:a", "copy", output_f]

            try:
                result = subprocess.run(burn_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                log(result.stdout)
                # Remove temporary subtitle file if it exists
                try:
                    if sub_idx is not None and os.path.exists(subtitle_file):
                        os.remove(subtitle_file)
                except Exception:
                    pass

                # Mark done in state
                mark_done(folder, f)

            except Exception as e:
                log(f"Error burning {f}: {e}")

            # update folder progress and global progress
            processed_in_folder += 1
            completed_tasks += 1
            folder_percent = (processed_in_folder / num_files) * 100 if num_files else 100
            global_percent = (completed_tasks / total_tasks) * 100 if total_tasks else 100

            ui_set_progress(folder, folder_percent)
            ui_set_global_progress(global_percent)

        # restore cwd
        try:
            os.chdir(prev_cwd)
        except Exception:
            pass

        log(f"Finished processing folder: {folder}")

    # all done
    ui_set_global_progress(100)
    ui_set_progress("", 100)  # harmless
    messagebox.showinfo("Done", "All folders done!")

# run in background thread
def threaded_run():
    t = threading.Thread(target=run, daemon=True)
    t.start()

# -----------------------
# UI: setup
# -----------------------
root = tk.Tk()
root.title("Recursive Subtitle Burner")
root.geometry("950x720")
root.resizable(True, True)
root.wm_minsize(width=700, height=480)

# Top buttons
button_frame = tk.Frame(root)
button_frame.pack(side="top", fill="x", padx=6, pady=6)
ttk.Button(button_frame, text="Select Folder", command=select_folder).pack(side="left", padx=6)
ttk.Button(button_frame, text="Start", command=threaded_run).pack(side="left", padx=6)

# Global progress bar
global_progress = ttk.Progressbar(button_frame, orient="horizontal", length=300, mode="determinate")
global_progress.pack(side="right", padx=6)
ttk.Label(button_frame, text="Overall:").pack(side="right")

# Scrollable area for folders
container = tk.Frame(root)
container.pack(fill="both", expand=True, padx=6, pady=6)

canvas = tk.Canvas(container, height=420)
scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
scrollable_frame = tk.Frame(canvas)

scrollable_frame.bind(
    "<Configure>",
    lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
)

canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
canvas.configure(yscrollcommand=scrollbar.set)

canvas.pack(side="left", fill="both", expand=True)
scrollbar.pack(side="right", fill="y")

def on_mousewheel(event):
    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
canvas.bind_all("<MouseWheel>", on_mousewheel)

# Log area
log_text = tk.Text(root, height=10, width=120)
log_text.pack(fill="both", expand=False, padx=6, pady=(0,6))

# Global state
folders = {}
log_queue = queue.Queue()
total_tasks = 0
completed_tasks = 0

# Periodic log updater (in case you want to extend with queue usage)
def update_log():
    # no-op here because ui_append_log uses root.after already
    root.after(200, update_log)

root.after(200, update_log)

root.mainloop()
