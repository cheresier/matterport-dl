---
agent: agent
description: Download a Matterport 3D tour by model ID and package it as a standalone Windows bundle
tools:
  - run_in_terminal
---

Download and package the Matterport model with ID **${input:modelId:Matterport model ID (e.g. nXa2VtHUZYa)}**.

The repo is at `d:\source\repos\matterport-dl`. All commands must be run from that directory.

## Step 1 — Download the model

Run the downloader and wait for it to finish completely before proceeding:

```powershell
cd d:\source\repos\matterport-dl
.\venv\Scripts\python.exe .\run.py ${input:modelId}
```

The download is complete when the script exits. It may take several minutes. Do not proceed to Step 2 until the command exits with code 0.

## Step 2 — Package the model

Run the packager, which builds `matterport-dl.exe` (once, via PyInstaller) and creates a self-contained bundle under `bundles\${input:modelId}\`:

```powershell
cd d:\source\repos\matterport-dl
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\package_model.ps1 -ModelId ${input:modelId}
```

This also:
- Fetches any missing webpack JS/CSS chunks and images from the Matterport CDN
- Copies all model data using robocopy (handles Windows MAX_PATH limits)
- Generates a `Launch.bat` so recipients can view the tour without Python

## Step 3 — Report the result

When packaging completes, report:
- The full path to the distributable zip: `d:\source\repos\matterport-dl\bundles\${input:modelId}.zip`
- That recipients extract the zip and double-click `Launch.bat` to view offline
- Any errors or warnings encountered during download or packaging
