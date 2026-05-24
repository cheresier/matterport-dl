---
description: Download a Matterport 3D tour by model ID and publish it online via Azure Blob Storage
---

Download and publish the Matterport model with ID **${input:modelId:Matterport model ID (e.g. nXa2VtHUZYa)}**.

The repo is at `d:\source\repos\matterport-dl`. All commands must be run from that directory.
Both commands are long-running (several minutes each). Use `mode=sync` with a generous timeout (e.g. 600000ms for the download, 1200000ms for the upload) so they can complete.

## Step 1 — Download the model

Run the downloader and wait for it to finish completely before proceeding:

```powershell
cd d:\source\repos\matterport-dl
.\venv\Scripts\python.exe .\run.py ${input:modelId}
```

The download is complete when the script exits with code 0. It may take several minutes. Do not proceed to Step 2 until the command exits with code 0.
If the command times out and moves to the background, use get_terminal_output to poll for completion. Do NOT start Step 2 until Step 1 has fully exited.

## Step 2 — Prepare and upload to Azure

Run the Azure uploader, which stages the model (patches HTML for static hosting) and uploads it to Azure Blob Storage:

```powershell
cd d:\source\repos\matterport-dl
.\venv\Scripts\python.exe .\prepare_for_azure.py ${input:modelId}
```

The script checks for an existing Azure session for the personal tenant and only prompts for
device-code login if needed. If it does prompt, tell the user to follow the on-screen instructions
in the terminal: open a browser, go to https://microsoft.com/devicelogin, and enter the code shown.
Wait for the upload to finish — it will print a URL when done. The upload can take 5-20 minutes for large models.

## Step 3 — Report the result

When the upload completes, report:
- The public URL printed at the end of the script output (click it to view the model in any browser)
- Any errors or warnings encountered during download or upload
