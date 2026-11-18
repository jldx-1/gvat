# GVAT source guide

GVAT provides a Linux entry point to CVAT's actual annotation page. The command
reads a flat media folder and CVAT XML directly. One shared `labels.txt` can
initialize class definitions for new XML. Review feedback persists in a portable
`reviews.json` beside the sample's media.

## Installation and shell integration

Run `./install.sh` from the repository root. Python 3.10+ with `venv` must be
available, including development headers for the pinned exporter engine.
Setup installs pinned dependencies in `.venv-gvat` and builds the
editor. The FFmpeg wheel includes a decoder on supported Linux platforms. Setup
downloads and verifies Node 22 LTS if Node 18+ is absent.
Both `python3` and `python` are supported; `GVAT_PYTHON=/path/to/python` explicitly
selects an interpreter for either root script. Shell aliases are not inherited
reliably. The scripts list their steps; download and cleanup helpers live under
`gvat/tools/`, with shared checks under `gvat/scripts/`.

Installation runs as your user without `sudo` or system package installation.
Both scripts print short status messages. Detailed output goes to
`.gvat-cache/install.log` and `.gvat-cache/uninstall.log`; failures report the
log path and final error line.

The installer adds a managed alias to `~/.bashrc` and links `~/.local/bin/gvat`.
Open a new Bash session or source the profile after installation.
`./uninstall.sh` removes only this checkout's alias, command link, environment,
and generated build. It preserves source code, media, XML, and class lists.
Missing Rust/C++ build tools are prepared in the user's cache, without modifying
the shell profile; `--purge-cache` removes those downloaded tools too.
The first install copies the repository root's `config.yml` to `$XDG_CONFIG_HOME/gvat/config.yml`
(default `~/.config/gvat/config.yml`). Existing config is preserved during
installation and uninstall. `GVAT_CONFIG` or `--config` selects another file.
Player step/pixel rendering, separate media/label export formats, and buffer
duration/memory defaults are validated before loading a sample. CLI flags win.

Options:

- `./install.sh --no-link --no-alias`: use `./src/bin/gvat` directly.
- `GVAT_BIN_DIR`: choose the command link directory.
- `GVAT_BASHRC`: choose the Bash profile to update.
- `./uninstall.sh --purge-cache`: also remove downloaded Node tools and media caches.

## Flat folders and XML

`--data_path` selects one flat folder. Pillow and FFmpeg identify files by content.
Only files directly in that folder are indexed. Image-only, video-only, and mixed
media are supported. Animated images and multi-page images expose native frames.
Optional decoder helpers must already be available; for example, EPS needs
Ghostscript. Missing or unreadable media produces an error before XML creation.

The XML is the annotation source. GVAT uses the folder's single `.xml` file or
offers to create `annotations.xml` if none exists. With multiple XML files,
select one through `--annotations` (also `--xml`). Earlier commands supplying an
XML through `--labels` remain accepted.

`--labels` (also `--classes`) selects a read-only text file with one class name
per line. New XML copies these names into its own metadata. Without that option,
GVAT looks for `labels.txt` in the annotation folder or its parent, then prompts for class
names. `--class-names=cat,dog` supplies names directly. A missing labels file
prompts for a corrected path. Existing XML uses embedded definitions; an explicit
class list must match them. GVAT never creates per-frame text files or label lists.

```bash
gvat --data_path="./example" --labels="./labels.txt" --mode=annotate
gvat --data_path="./example" --mode=review
```

`--yes` accepts XML creation with supplied/discovered classes. Missing media or
classes still fails noninteractively with an actionable message. `--mode=auto`
offers review when XML has annotations; new or class-only XML opens annotation
mode. Explicit `annotate` and `review` modes avoid that choice.

CVAT's own annotation models handle drawing, interpolation, grouping, labeling,
attributes, and undo/redo. The local adapter translates their serialized collection
to CVAT XML. Boxes, polygons, polylines, points, ellipses, and tags are supported.
Image names must match the selected files. Track frame numbers retain their
original zero-based source indices, including task frame filters. The native
frame player and object editing semantics are unchanged.

New XML follows CVAT 1.1, including task details, timestamps, labels, segments,
owner fields, and dump time. Video XML includes `original_size`; image entries
include dimensions and lexical image IDs. CVAT 1.0 imports without dimensions
read their dimensions from the matching source media.

New XML for one video uses tracks; other new XML uses image entries. Video shapes
are written as single-frame tracks, matching CVAT's video export format. Native
track IDs, attributes, geometry, and metadata are retained. No-op saves retain
bytes and modification times. Changed XML is replaced atomically with revision
checks. Flipped and multi-task video exports require separate preparation.

## Runtime and review

Video frames are indexed by source content hash, presentation timestamps, and
keyframes. CVAT's native frame decoder, chunk cache, and prefetcher load lossless
image batches. A video batch is decoded in one FFmpeg process; seeking starts at
an indexed keyframe and preserves original frame numbers, including variable
frame rates and XML frame filters. Video indexes from older installations are
rebuilt once on first use.

Scrubbing cancels obsolete HTTP requests and FFmpeg work so the newest position
gets priority. Continuous playback keeps useful prefetch work. A disconnected
browser does not trigger another error response on its closed connection.
Chunk sizes target at most 32 MiB of decoded pixels, up to 32 frames. The decoded
cache targets 15 seconds of native video frames, rounded to whole chunks, and
uses at most half of available RAM (including a container memory limit). It can
span several GiB; `--buffer-seconds` changes the duration and `--buffer-memory`
sets a smaller upper limit. At least one frame/chunk is retained. The separate
server transfer cache retains at most 64 MiB/eight chunks. Video decoding runs
outside the annotation lock.

Previews, indexes, and process locks live under `$XDG_CACHE_HOME/gvat` (default
`~/.cache/gvat`), outside the annotation folder. Originals are never rewritten. Source media
changes require restarting GVAT. External XML edits prompt a reload; stale saves
are rejected. One GVAT process can write a particular XML at a time.

The server listens on `127.0.0.1`. Open its complete printed session URL.
`--no-browser` prints the URL only; `--port=0` chooses a free port.
Save your annotations and press Ctrl+C in the terminal to stop GVAT; closing the
browser tab alone leaves the server running. The same port can be reused
immediately after stopping. If another instance is running, stop it or choose a
different port with `--port=0`.
Use CVAT's toolbar, native player, Objects/Labels/Issues tabs, and workspace
selector. The Save button and Ctrl+S write XML. Shortcut bindings are CVAT's own.
Open the native settings dialog with F2 or Menu → Settings. Under Player, disable
Smooth image to see pixelated zoom. This is CVAT's existing display option,
stored in browser local storage; source media and XML are unchanged.

Review findings reference a box, region, or whole frame. Findings, replies and
resolved state are atomically saved in `reviews.json` in the sample folder.
They survive process restarts and moving the folder because frame identities
are media-relative. Invalid review files are reported, never silently replaced.
**Menu → Copy feedback** creates Markdown for a merge request. If clipboard
access fails, copy the text from the native dialog.
Reviewing does not modify XML. No `.gvat` directory is created.

## Annotation-only entry point

The web entry imports `components/annotation-page/annotation-page` directly from
`src/cvat/cvat-ui`. CVAT's Redux reducers, core annotation collection/history,
canvases, toolbars, object sidebar, and standard, attribute, single-shape, tag, and
review workspaces run unchanged. The application shell and its dashboard, task,
project, and account routes are not mounted.

`gvat/web/src/native/` supplies the host, media transport, and local storage bridge.
`gvat/src/gvat_local/native.py` translates CVAT collections to XML and keeps review
issues in the sample's review file. A guarded upstream annotation-menu change removes unrelated
server-management actions and adds Copy feedback. See [UPSTREAM.md](UPSTREAM.md).

## Annotation exports

Menu → Export labels downloads annotations only. Menu → Export dataset also
includes images or decoded video frames. Both mount CVAT's actual export dialog
and offer all 29 enabled image/video formats from the pinned CVAT release.
Audio and point-cloud formats belong to other CVAT workspaces.

The catalog and transform order come from `cvat/apps/dataset_manager/formats`.
Conversions use the same pinned CVAT fork of Datumaro and upstream standalone
transform classes. CVAT's own core expands tracks before streaming frame records
to the local exporter, preserving interpolation and original frame numbers.
Formats retain the annotation types they support, as in CVAT.
Exports are revision checked, prepared in the runtime cache, and downloaded as
ZIP files. They do not modify the selected XML, media or shared label list.
Matching CVAT XML exports retain the saved bytes; converted CVAT XML uses the
same XML writer as Save. Temporary export files are removed after download.

`--export-labels=OUTPUT.zip` and `--export-dataset=OUTPUT.zip` run headlessly.
`--format="FORMAT"` overrides the corresponding config default;
`--list-formats` lists names. CLI exports reuse the same local export jobs and
pinned converters. The original pure `TrackManager.get_interpolated_shapes`
method from upstream CVAT expands tracks without importing its Django bindings.
Output is streamed to a sibling temporary file and published atomically. Existing
archives require `--overwrite`; source media, XML and review files are protected.

## SSH locations

`--data_path` accepts `ssh://[user@]host[:port]/path` and `[user@]host:/path`.
The client uses Paramiko, verified known-host keys, OpenSSH host/key settings,
agent authentication, optional interactive password/passphrase, and jump/proxy
connections. SFTP is the only remote service used; no remote GVAT install is needed.
Local/SSH shared class lists are supported. Remote XML can be selected by its
filename in the remote sample or by a URL using the same endpoint.

Each remote sample has a locked cache under `$XDG_CACHE_HOME/gvat/ssh`. Regular
files in its flat directory are cached before opening. Unchanged media is reused
by remote size/mtime. XML and review metadata refresh from the server; mutations
check revisions and use `posix-rename` atomic replacement. Changed remote media
requires a restart. No source media is written back. `--purge-cache` removes the
local SSH cache, preserving the authoritative remote files.

## Development

From the repository root after installation:

```bash
.venv-gvat/bin/python -m unittest discover -s src/gvat/tests -v
cd src/gvat/web
npm run build
npx playwright install chromium
npm run test:e2e
```

Tests cover XML persistence, native frames, media decoding, flat-folder discovery,
conflicts, persistent native review, real loopback SSH/SFTP transfers, config
defaults, headless exports, Bash aliases, isolated installation cleanup, and
canvas interactions. They do not uninstall the working checkout or edit the real
Bash profile. The lower-level YOLO parser remains an internal validation utility;
the CLI uses CVAT XML.

Optional recipes are in `src/justfile`. Upstream source and its MIT license are
preserved in `src/cvat/`. See [UPSTREAM.md](UPSTREAM.md) and the
[CVAT README](cvat/README.md).
