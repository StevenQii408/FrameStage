# Changelog

## v0.1.2 — canvas media preview

- Double-click an image or video to frame it with the canvas camera. Pan or zoom while inspecting the asset and close the preview to return to the previous view.
- Toggle the minimap from the bottom left. The minimap starts open, stays above the canvas and keeps the visibility you choose while previewing media.
- Download the displayed image or video version from the selected card or its preview.

## v0.1.1 — FrameStage

- Renamed FrameForge Studio to FrameStage. The video creation workbench supports AI-assisted storyboarding from reference videos and scripts.

## v0.1.0 — release hardening

- Image and video generation have optional cloud adapters, a persistent local queue, SSE progress and separate remote cancellation. Real generation is off by default.
- Export only the verified commit; original cloud responses, local troubleshooting notes and the entire showcase directory are not source distribution assets.

- Added local API session protection, server-side Origin checks and Host allowlisting; browser clients bootstrap an in-memory token and CLI clients use an explicit local token.
- Closed the analysis SSE terminal-event race by draining persisted events before ending the stream.
- Hardened task cancellation, execution deadlines, local edit/remote state separation and local media persistence. These changes do not authorize a previously cancelled paid request to restart.
- Added an explicit MIT license for project-authored work, security and contribution policies, reproducible dependency installation and key-free CI checks.
- Moved historical implementation notes out of the current Quick Start. Old strategy descriptions are historical records, not current default behavior.

## 2026-11-11 — source evidence and media preview

- Preserved real source cut boundaries instead of forcing 4.8-second source shots. Long sources use internal analysis spans; source timestamps remain distinct from half-second generation timing.
- Separated static/dynamic review states and retained valid observations during bounded protocol correction.
- Added a larger original-video player with real cut markers, in-node asset preview and first/current/last-frame capture to local reference nodes.

## 2026-11-10 — project workspaces

- Added a project home, independent canvases, revision-based saving, recovery drafts and a project recycle bin.
- Consolidated node details into an on-demand host and generation editing into a single compact node composer.
- Retired local MLX inference. Analysis uses cloud Flash or an explicitly selected Mock provider; image/video generation remains disconnected.

Historical investigation and migration notes are kept in `docs/history/`. They may describe constraints and UI layouts superseded by the current code.
