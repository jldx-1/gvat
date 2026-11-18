"""Tiny real videos used by CLI, workspace, and browser acceptance tests."""

import subprocess

import imageio_ffmpeg
from PIL import Image


def make_video(path, frames=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if frames is None:
        frames = [Image.new("RGB", (64, 48), color) for color in ("red", "green", "blue")]
    width, height = frames[0].size
    codec = "libvpx-vp9" if path.suffix.lower() == ".webm" else "mpeg4"
    quality = ["-b:v", "0", "-crf", "30"] if codec == "libvpx-vp9" else ["-q:v", "2"]
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error", "-threads", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "2",
        "-i", "pipe:0", "-an", "-c:v", codec, "-threads", "1", *quality, str(path),
    ]
    subprocess.run(command, input=b"".join(frame.convert("RGB").tobytes() for frame in frames), check=True, capture_output=True)
