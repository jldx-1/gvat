"""Linux launcher for an account-free, file-backed CVAT canvas."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import signal
import sys
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .workspace import WorkspaceError, atomic_write, load_classes
from .media import discover_media
from .cvat_xml import CvatDocument, CvatXmlWorkspace
from .buffering import memory_size
from .ssh import input_locations, location
from .configuration import load_config


def parser(settings=None) -> argparse.ArgumentParser:
    settings = settings or load_config()
    result = argparse.ArgumentParser(prog="gvat", description="Label or review a local directory using the CVAT canvas. No Docker, accounts, or Git integration.")
    result.set_defaults(settings=settings)
    result.add_argument('--config', type=Path, help='Use a particular config.yml instead of the user defaults')
    result.add_argument("--version", action="version", version=f"gvat {__version__}")
    result.add_argument("--data_path", "--data-path", type=location, help="Flat sample folder, local or ssh://[user@]host[:port]/path")
    result.add_argument("--labels", "--classes", type=location, help="Local/SSH shared labels.txt; used to initialize missing XML")
    result.add_argument("--annotations", "--xml", type=location, help="CVAT XML file (default: discover the single XML in DATA_PATH, or create annotations.xml)")
    result.add_argument("--class-names", help="Comma-separated class names for initializing missing XML")
    result.add_argument("--mode", choices=("auto", "annotate", "review"), default="auto")
    result.add_argument('--step', type=int, default=settings['player']['step'], help='Native player step size in frames')
    display = result.add_mutually_exclusive_group()
    display.add_argument('--pixelated', action='store_true', default=settings['player']['pixelated'], help='Disable CVAT smooth image rendering')
    display.add_argument('--smooth-images', dest='pixelated', action='store_false', help='Enable CVAT smooth image rendering')
    result.add_argument("--buffer-seconds", type=float, default=settings['buffer']['seconds'], help="Decoded video buffer target in seconds (default from config; capped by available RAM)")
    result.add_argument("--buffer-memory", type=memory_size, default=memory_size(str(settings['buffer']['max_memory'])) if settings['buffer']['max_memory'] is not None else None, help="Upper memory limit for the decoded buffer, e.g. 4GiB")
    result.add_argument("--port", type=int, default=8080, help="Loopback port (default: 8080; 0 chooses an available port)")
    result.add_argument("--no-browser", action="store_true", help="Print the session URL without opening a browser")
    result.add_argument("--yes", "-y", action="store_true", help="Create missing XML using supplied/discovered class names")
    exports = result.add_mutually_exclusive_group()
    exports.add_argument('--export-labels', type=Path, metavar='OUTPUT.zip', help='Export annotations and exit without opening the browser')
    exports.add_argument('--export-dataset', type=Path, metavar='OUTPUT.zip', help='Export annotations with media and exit')
    result.add_argument('--format', '--export-format', help='Exact CVAT export format name, e.g. "YOLO 1.1"')
    result.add_argument('--list-formats', action='store_true', help='List available CVAT image/video export formats and exit')
    result.add_argument('--overwrite', action='store_true', help='Replace an existing CLI export destination')
    return result


def ask(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise WorkspaceError(f"{prompt} Supply the corresponding CLI option when running non-interactively.")
    return input(prompt + " ").strip()


def directory(path: Path | None, name: str, create: bool = False, yes: bool = False) -> Path:
    while True:
        if path is None:
            answer = ask(f"{name} directory:")
            if not answer:
                continue
            path = Path(answer)
        path = path.expanduser().resolve()
        if path.is_dir():
            return path
        if create and not path.exists():
            answer = "y" if yes else ask(f"{path} does not exist. Create it? [y/N], or enter a corrected path:")
            if answer.lower() in ("y", "yes"):
                path.mkdir(parents=True)
                return path
            if answer and answer.lower() not in ("n", "no"):
                path = Path(answer)
                continue
        answer = ask(f"{name} directory is missing or invalid. Enter a corrected path:")
        path = Path(answer) if answer else None


def class_names(names: str) -> list[str]:
    entries = [name.strip() for name in names.split(",")]
    if any(not name or "\n" in name or "\r" in name for name in entries) or len(entries) != len(set(entries)):
        raise WorkspaceError("Class names must be nonempty and unique")
    return entries


def choose_classes(args: argparse.Namespace, data: Path) -> list[str]:
    if args.class_names:
        return class_names(args.class_names)
    classes = args.labels
    if classes is None:
        classes = next((root / "labels.txt" for root in (data, data.parent) if (root / "labels.txt").is_file()), None)
    while True:
        if classes is not None:
            classes = classes.expanduser().resolve()
            if classes.is_file() and classes.suffix.lower() == ".txt":
                return list(load_classes(classes)[0].values())
            answer = ask(f"Labels file {classes} is missing or invalid. Enter a corrected labels.txt path:")
            classes = Path(answer) if answer else None
            continue
        answer = ask("No class definitions found. Enter a labels.txt path or comma-separated class names:")
        if not answer:
            continue
        candidate = Path(answer).expanduser()
        if candidate.is_file() or candidate.suffix or "/" in answer or "\\" in answer:
            classes = candidate
        else:
            return class_names(answer)


def annotation_file(args: argparse.Namespace, data: Path) -> Path:
    selected = args.annotations
    # Accept earlier commands that supplied an XML file through --labels.
    if args.labels and args.labels.suffix.lower() == ".xml":
        if selected:
            raise WorkspaceError("Specify the XML once, using --annotations")
        selected, args.labels = args.labels, None
    if selected is None:
        exports = sorted(path for path in data.iterdir() if path.is_file() and path.suffix.lower() == ".xml")
        if len(exports) > 1:
            answer = ask("Multiple XML files found. Supply --annotations or enter the XML filename/path:")
            selected = data / answer if (data / answer).is_file() else Path(answer)
        else:
            selected = exports[0] if exports else data / "annotations.xml"
    selected = selected.expanduser().resolve()
    if selected.suffix.lower() != ".xml" or selected.is_dir():
        raise WorkspaceError("--annotations must select a CVAT XML file")
    return selected


def prepare(args: argparse.Namespace) -> CvatXmlWorkspace:
    data = directory(args.data_path, "Media")
    candidates = discover_media(data, recursive=False)
    selected = annotation_file(args, data)
    created = False
    while not selected.is_file():
        answer = "y" if args.yes and not selected.exists() else ask(f"XML file {selected} is missing. Create it? [y/N], or enter a corrected XML path:")
        if answer.lower() in ("y", "yes") and not selected.exists():
            atomic_write(selected, CvatDocument.for_media(choose_classes(args, data), candidates), "missing")
            created = True
            break
        if answer and answer.lower() not in ("n", "no"):
            selected = Path(answer).expanduser().resolve()
            if selected.suffix.lower() != ".xml" or selected.is_dir():
                raise WorkspaceError("Select a CVAT .xml file")
    workspace = CvatXmlWorkspace(data, selected, candidates=candidates, native_ui=True)
    if not created and (args.labels or args.class_names) and not getattr(args, '_discovered_labels', False):
        if choose_classes(args, data) != list(workspace.document().classes.values()):
            raise WorkspaceError("labels.txt does not match the XML's classes. Update the class list or omit --labels to use the XML definitions.")
    manifest = workspace.manifest()
    print(f"{len(workspace.sources)} media sources; {len(manifest['images'])} images/frames. Annotations: {selected}")
    mode = args.mode
    if mode == "auto":
        mode = "annotate"
        if not created and manifest["has_annotations"]:
            answer = ask("Existing labels found. [R]eview or [a]nnotate? (default: review)") if sys.stdin.isatty() else "r"
            if answer.lower() not in ("", "r", "review", "a", "annotate"):
                raise WorkspaceError("Select review or annotate, or pass --mode explicitly")
            mode = "annotate" if answer.lower() in ("a", "annotate") else "review"
    workspace.mode = mode
    workspace.buffer_seconds = args.buffer_seconds
    workspace.buffer_memory = args.buffer_memory
    workspace.settings = {'frame_step': args.step, 'pixelated': args.pixelated, **args.settings['export']}
    return workspace


@contextmanager
def workspace_lock(labels: Path):
    import fcntl

    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "gvat" / "locks"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.fsencode(labels)).hexdigest()
    with (cache / f"{key}.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkspaceError("Another GVAT instance is already using this labels directory") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main() -> None:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument('--config', type=Path)
    selection, _ = bootstrap.parse_known_args()
    try:
        settings = load_config(selection.config)
    except WorkspaceError as error:
        bootstrap.error(str(error))
    arguments = parser(settings)
    args = arguments.parse_args()
    if not 1 <= args.step <= 10000:
        arguments.error('--step must be between 1 and 10000')
    if not 0 < args.buffer_seconds <= 3600:
        arguments.error("--buffer-seconds must be greater than 0 and at most 3600")
    if sys.platform != "linux":
        arguments.exit(2, "GVAT currently supports Linux only. Run ./install.sh and gvat from a Linux shell.\n")
    if not 0 <= args.port <= 65535:
        arguments.error("--port must be between 0 and 65535")
    from .exports import catalog

    if args.list_formats:
        print('\n'.join(sorted(catalog())))
        return
    destination = args.export_labels or args.export_dataset
    if args.format and not destination:
        arguments.error('Use --format together with --export-labels=OUTPUT.zip or --export-dataset=OUTPUT.zip')
    if destination and destination.suffix.lower() != '.zip':
        arguments.error('CLI exports are ZIP archives; choose an output filename ending in .zip')
    if args.format and args.format not in catalog():
        arguments.error('Unknown export format. Use --list-formats to see the exact names.')
    if args.overwrite and not destination:
        arguments.error('--overwrite applies only to CLI exports')
    assets = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not destination and not (assets / "index.html").is_file():
        arguments.exit(2, "The local editor has not been built. Run ./install.sh from the repository root.\n")
    try:
        with input_locations(args) as remote:
            if destination:
                args.mode = 'annotate'
            workspace = prepare(args)
            if remote:
                remote.bind(workspace)
            annotation_path = workspace.xml_file
            with workspace_lock(annotation_path):
                if destination:
                    from .export_cli import export

                    selected_format = args.format or settings['export']['dataset_format' if args.export_dataset else 'labels_format']
                    if selected_format == 'auto':
                        selected_format = 'CVAT for video 1.1' if workspace.document().root.findtext('meta/task/mode') == 'interpolation' else 'CVAT for images 1.1'
                    export(workspace, destination, selected_format, bool(args.export_dataset), args.overwrite)
                else:
                    serve(workspace, assets, args)
    except (WorkspaceError, OSError, EOFError) as error:
        arguments.exit(2, f"gvat: {error}\n")
    except KeyboardInterrupt:
        arguments.exit(130, "\ngvat: canceled\n")


def serve(workspace, assets, args):
    from .server import LocalServer

    annotation_path = workspace.xml_file
    with LocalServer(workspace, assets, secrets.token_urlsafe(32), args.port) as server:
        url = f"{server.origin}/#token={server.token}"
        destination = workspace.source_root / 'reviews.json' if workspace.mode == 'review' else annotation_path
        print(f"GVAT {workspace.mode} mode. Changes save to {destination}")
        print(f"Open {url}", flush=True)
        print("Press Ctrl+C to stop. Unsaved browser edits must be saved before stopping.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        def stop(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nGVAT stopped.")
