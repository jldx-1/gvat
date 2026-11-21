# Upstream source

This workspace includes CVAT Community **v2.83.0**, cloned from
[cvat-ai/cvat](https://github.com/cvat-ai/cvat) at commit
`678221a53665cded90e20822ba50704fd21e44ca`.

The original CVAT license and copyright notices remain in the source tree.

GVAT's Linux filesystem workflow is in `src/gvat/`, with `install.sh` and
`uninstall.sh` at the root, the launcher in `src/bin/`, and the optional
`justfile` in `src/`. Upstream sources are preserved in `src/cvat/`.
GVAT mounts CVAT's actual `cvat-ui/src/components/annotation-page/annotation-page.tsx`
directly. Its annotation, attribute-labeling, single-shape, tag, and review
workspaces use upstream components, Redux state, annotation models, and history.
The frame transport also uses CVAT's native chunk decoder, bounded cache, and
prefetcher, backed by local lossless batches and cancellable indexed video seeks.
The full application shell is not mounted.

Local UI source changes are guarded by `GVAT_LOCAL`: the annotation menu removes
task/server-management actions, adds Copy feedback and access to CVAT's existing
Settings dialog (also available through F2), and opens native exports with or
without media. The export modal hides remote storage and request-dashboard links.
The local host supplies configured defaults to the existing player settings and
export modal; no replacement player or annotation tools are introduced.
A guarded core frame-cache policy accepts the host's RAM-capped buffer budget.
The same guarded path fills the native decoded cache forward while paused or
playing, rechecks the selected frame between chunks, and gives seeks priority.
Frame decoder fixes release partial bitmaps on failure/close, settle superseded
requests, and detach completed worker callbacks. The ZIP worker transfers bitmap
ownership and decodes sequentially to avoid duplicate pixel storage.
The local entry
point and transport live under `src/gvat/`; no replacement annotation screen is
maintained. Media previews and XML persistence are provided by the local Python
process. CVAT's Django/database/queue deployment is not required.

Annotation exports use CVAT's pinned Datumaro engine at commit
`335fc3e55fe699b9401091f6af11f10c3b0e08cb` and the export catalog and standalone
transformations from this CVAT tree. The local adapter supplies in-memory
annotation states and local media in place of CVAT's database bindings.
Headless CLI exports use CVAT's original pure track interpolation method from
`cvat/apps/dataset_manager/annotation.py` without importing its Django bindings.
SSH caching, metadata persistence and the portable review file are local adapter
features under `src/gvat/`.
