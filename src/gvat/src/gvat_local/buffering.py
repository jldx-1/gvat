"""Size the decoded video buffer by duration and the RAM currently available."""

import math
import os
import re
import statistics
from pathlib import Path


def memory_size(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|TiB|KB|MB|GB|TB|B)?\s*", value, re.I)
    if not match:
        raise ValueError("Use a memory size such as 512MiB or 4GiB")
    suffix = (match[2] or 'B').lower()
    power = {'b': 0, 'kb': 1, 'kib': 1, 'mb': 2, 'mib': 2, 'gb': 3, 'gib': 3, 'tb': 4, 'tib': 4}[suffix]
    size = int(float(match[1]) * (1024 if 'i' in suffix else 1000) ** power)
    if size <= 0:
        raise ValueError("Buffer memory must be positive")
    return size


def available_memory():
    limits = []
    try:
        line = next(line for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        limits.append(int(line.split()[1]) * 1024)
    except (OSError, ValueError, StopIteration):
        limits.append(os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE'))
    for maximum, current in (('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.current'),
                             ('/sys/fs/cgroup/memory/memory.limit_in_bytes', '/sys/fs/cgroup/memory/memory.usage_in_bytes')):
        try:
            limits.append(max(0, int(Path(maximum).read_text()) - int(Path(current).read_text())))
        except (OSError, ValueError):
            pass
    return min(limits)


def buffer_plan(workspace, frames, chunk_size):
    bytes_per_frame = max(frame['width'] * frame['height'] * 4 for frame in frames)
    rates = []
    for index in workspace.video_indexes.values():
        base = index.get('time_base')
        if not base:
            continue
        deltas = [(b - a) * base[0] / base[1] for a, b in zip(index['pts'], index['pts'][1:])
                  if a is not None and b is not None and b > a]
        if deltas:
            rates.append(1 / statistics.median(deltas))
    fps = max(rates, default=25)
    seconds = getattr(workspace, 'buffer_seconds', 15)
    target_frames = min(len(frames), math.ceil(fps * seconds))
    # Round to whole chunks so a 15-second target does not lose almost an
    # entire chunk solely because its last few frames cross the boundary.
    wanted = max(bytes_per_frame, min(len(frames), math.ceil(target_frames / chunk_size) * chunk_size) * bytes_per_frame)
    available = available_memory()
    # Leave half of free RAM for the browser, annotations, FFmpeg and other apps.
    cap = max(bytes_per_frame, available // 2)
    override = getattr(workspace, 'buffer_memory', None)
    if override is not None:
        cap = min(cap, max(bytes_per_frame, override))
    budget = min(wanted, cap)
    chunks = max(1, min(math.ceil(target_frames / chunk_size), budget // (bytes_per_frame * chunk_size)))
    return {'seconds': seconds, 'fps': fps, 'max_bytes': budget, 'max_chunks': chunks,
            'prefetch_chunks': max(0, chunks - 1), 'available_bytes': available,
            'target_bytes': wanted, 'capped': budget < wanted}
