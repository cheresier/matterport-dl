#!/usr/bin/env python3
"""Stages and uploads a downloaded Matterport model to Azure Blob Storage
for static web hosting.  No local server required after upload.

Usage:
  python prepare_for_azure.py <modelId>                          # stage + upload + cleanup
  python prepare_for_azure.py <modelId> --stage-only             # stage only, no upload
  python prepare_for_azure.py <modelId> --tenant <tenant-id>     # login to personal/other tenant first

What it does:
  1. Builds a clean staging folder from downloads/<modelId>/:
     - .modified. files replace their non-modified counterparts
       (index.modified.html -> index.html, graph_X.modified.json -> graph_X.json, etc.)
     - Injects a GraphQL POST interceptor into index.html so the viewer
       works without a Python server (POSTs are served from cached JSON files)
     - Fixes window._ProxyBase so relative paths work in a subfolder URL
  2. Uploads the staging folder to Azure Blob Storage ($web/<modelId>/)
  3. Deletes the staging folder
  4. Prints the public URL
"""

import os
import re
import sys
import shutil
import subprocess
import pathlib
import time

from curl_cffi import requests as _curl_requests

# Azure CLI on Windows is az.cmd, not az — shutil.which resolves via PATHEXT
_AZ_EXE = shutil.which("az")
if _AZ_EXE is None:
    sys.exit(
        "Azure CLI (az) not found in PATH.\n"
        "Install from: https://aka.ms/installazurecli"
    )

# ── Config ────────────────────────────────────────────────────────────────────

STORAGE_ACCOUNT = "samatterportstaticweb"
TENANT          = "d08eac19-de40-4e5e-b3dd-0fa00933c5bc"  # personal Azure tenant
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
STAGING_BASE = SCRIPT_DIR / "_azure_staging"

# Signed CDN URLs have validUntil timestamps that expire within hours.
# Replace with a far-future date so the static-hosted viewer never
# stalls on the [expiring-resource] refresh loop.
FAR_FUTURE_DATE = "2099-12-31T23:59:59Z"
FAR_FUTURE_EPOCH = 4102444799  # 2099-12-31T23:59:59Z as Unix timestamp
# Plain JSON:   "validUntil":"2026-05-20T20:55:05Z"
_VALID_UNTIL_RE = re.compile(r'"validUntil"\s*:\s*"[^"]*"')
# Escaped JSON inside JS string literals: \"validUntil\":\"DATE\"
_VALID_UNTIL_ESC_RE = re.compile(r'\\"validUntil\\"\s*:\s*\\"[^"]*\\"')

_EXPIRES_RE = re.compile(r'"expires"\s*:\s*\d+')
_EXPIRES_ESC_RE = re.compile(r'\\"expires\\"\s*:\s*\d+')

_VALID_UNTIL_ISO = re.compile(
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z'
)

def _fix_valid_until(text: str) -> tuple[str, int]:
    """Replace all validUntil date values with FAR_FUTURE_DATE.

    Works for both plain JSON (graph_*.json) and escaped JSON
    embedded in HTML (MP_PREFETCHED_MODELDATA inside index.html).
    """
    # Plain JSON: "validUntil":"2026-05-20T20:55:05Z"
    fixed, n1 = _VALID_UNTIL_RE.subn(f'"validUntil":"{FAR_FUTURE_DATE}"', text)
    # Escaped JSON (inside JS string): \"validUntil\":\"DATE\"
    fixed, n2 = _VALID_UNTIL_ESC_RE.subn(
        f'\\"validUntil\\":\\"{FAR_FUTURE_DATE}\\"', fixed
    )
    return fixed, n1 + n2


def _fix_expires(text: str) -> tuple[str, int]:
    """Replace "expires":TIMESTAMP with a far-future epoch."""
    fixed, n1 = _EXPIRES_RE.subn(f'"expires":{FAR_FUTURE_EPOCH}', text)
    fixed, n2 = _EXPIRES_ESC_RE.subn(
        f'\\"expires\\":{FAR_FUTURE_EPOCH}', fixed
    )
    return fixed, n1 + n2


# Files that serve no purpose in static hosting
SKIP_NAMES = {
    "run_report.log",
    "run_args.json",
    "matterport-dl.py",
    "_matterport_interactive.py",
}

# Injected after JSNetProxy.js.
# 1. Patches window._replaceHost to handle three URL classes:
#    a) Already has origin+subfolder prefix → leave alone (prevent doubling)
#    b) Has origin but no subfolder (showcase builds URLs as _ProxyBase+"/api/...") →
#       insert subfolder so it resolves correctly inside the model folder
#    c) Foreign URL (api.matterport.com etc.) → replace host then insert subfolder
# 2. Intercepts GraphQL requests (POST and GET/APQ) to api/mp/models/graph and
#    serves pre-downloaded JSON files.
#    - POST: parse operationName from body
#    - GET (Apollo APQ with useGETForHashedQueries=true): parse operationName from
#      the URL query string.  The bare 'graph' file in the download contains
#      {"data":"empty"} which caused every APQ query to fail immediately.
POST_INTERCEPTOR = (
    "<script>\n"
    "/* Static hosting shim: subfolder fix + GraphQL POST/GET intercept */\n"
    "(function () {\n"
    "  var _origin    = window.location.origin;\n"
    "  var _subfolder = window.location.pathname.replace(/\\/?$/, '');\n"
    "  if (_subfolder && window._replaceHost) {\n"
    "    var _origRH = window._replaceHost;\n"
    "    window._replaceHost = function (str) {\n"
    "      if (!str || typeof str !== 'string') return _origRH(str);\n"
    "      /* Already has our subfolder prefix — leave it alone */\n"
    "      if (str.startsWith(_origin + _subfolder + '/') || str === _origin + _subfolder)\n"
    "        return str;\n"
    "      /* On our origin but no subfolder yet — insert it */\n"
    "      if (str.startsWith(_origin + '/') || str === _origin)\n"
    "        return _origin + _subfolder + str.slice(_origin.length);\n"
    "      /* Foreign URL — replace host then insert subfolder */\n"
    "      var out = _origRH(str);\n"
    "      if (out !== str && out.startsWith(_origin))\n"
    "        return _origin + _subfolder + out.slice(_origin.length);\n"
    "      return out;\n"
    "    };\n"
    "  }\n"
    "  var _orig = window.fetch;\n"
    "  var _emptyGql = new Response('{\"data\":{}}',\n"
    "        {status:200, headers:{'Content-Type':'application/json'}});\n"
    "  function _gqlFetch(op) {\n"
    "    console.log('[shim] graph fetch:', op);\n"
    "    return _orig('api/mp/models/graph_' + op + '.json').then(function(r) {\n"
    "      if (!r.ok) {\n"
    "        console.warn('[shim] graph 404, returning empty data for:', op);\n"
    "        return new Response('{\"data\":{}}',\n"
    "          {status:200, headers:{'Content-Type':'application/json'}});\n"
    "      }\n"
    "      return r.clone().json().then(function(j) {\n"
    "        if (j.errors && !j.data) {\n"
    "          console.warn('[shim] graph error response, returning empty data for:', op, j.errors);\n"
    "          return new Response('{\"data\":{}}',\n"
    "            {status:200, headers:{'Content-Type':'application/json'}});\n"
    "        }\n"
    "        return r;\n"
    "      });\n"
    "    });\n"
    "  }\n"
    "  window.fetch = function (url, opts) {\n"
    "    var s = typeof url === 'string' ? url\n"
    "          : (url instanceof Request) ? url.url : String(url);\n"
    "    if (/\\/api\\/mp\\/models\\/graph(\\?|$)/.test(s)) {\n"
    "      var method = opts ? String(opts.method || '').toUpperCase() : 'GET';\n"
    "      if (method === 'POST') {\n"
    "        try {\n"
    "          var b = typeof opts.body === 'string' ? JSON.parse(opts.body) : opts.body;\n"
    "          if (b && b.operationName) return _gqlFetch(b.operationName);\n"
    "        } catch (_) {}\n"
    "      } else {\n"
    "        try {\n"
    "          var u = new URL(s, window.location.href);\n"
    "          var op = u.searchParams.get('operationName');\n"
    "          if (op) return _gqlFetch(op);\n"
    "        } catch (_) {}\n"
    "      }\n"
    "    }\n"
    "    return _orig.apply(this, arguments);\n"
    "  };\n"
    "}());\n"
    "</script>"
)

# ── Chunk backfill ────────────────────────────────────────────────────────────

def _backfill_chunks(model_dir: pathlib.Path) -> None:
    """Download missing lazy-loaded JS/CSS chunks and static files from CDN.

    The matterport-dl downloader captures named webpack chunks but can miss
    numbered code-split chunks that are only lazy-loaded at runtime.  This
    function parses runtime~showcase.js to discover all referenced chunk IDs
    and fetches any that are absent from the download folder.
    """
    js_dir = model_dir / "js"
    css_dir = model_dir / "css"
    runtime_file = js_dir / "runtime~showcase.js"
    index_file = model_dir / "index.html"

    if not runtime_file.exists() or not index_file.exists():
        return

    # Derive CDN base URL from <base href> in the original index.html
    index_text = index_file.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'base href="([^"]+)"', index_text)
    if not m:
        return
    cdn_base = m.group(1).rstrip("/")

    runtime = runtime_file.read_text(encoding="utf-8", errors="ignore")

    # ── JS chunks ─────────────────────────────────────────────────────────
    # Named chunk IDs already have human-readable filenames (e.g. "core.js")
    named_js = {mat.group(1) for mat in re.finditer(r'(\d+):"[a-z]', runtime)}

    # All JS chunk IDs referenced by lazy-load calls: X.e(N)
    all_js: set[str] = set()
    for f in js_dir.glob("*.js"):
        content = f.read_text(encoding="utf-8", errors="ignore")
        for mat in re.finditer(r'\b[a-zA-Z_$]{1,2}\.e\((\d+)\)', content):
            all_js.add(mat.group(1))
        # Also detect webpack context-module chunks: "./file":[moduleId, chunkId]
        # These are lazy-loaded via i.e(t[1]) where t is a variable, so the
        # numeric .e(N) pattern above won't catch them.
        for mat in re.finditer(r'"\.\/[^"]+"\s*:\s*\[\d+\s*,\s*(\d+)\]', content):
            all_js.add(mat.group(1))

    missing_js = [
        cid for cid in sorted(all_js - named_js, key=int)
        if not (js_dir / f"{cid}.js").exists()
    ]

    # ── CSS chunks ────────────────────────────────────────────────────────
    missing_css: list[str] = []
    css_avail = re.search(r'&&(\{(?:\d+:1,?)+\})\[', runtime)
    if css_avail:
        css_ids = {mat.group(1) for mat in re.finditer(r'(\d+):1', css_avail.group(1))}
        css_named_match = re.search(r'miniCssF[^{]*\{([^}]+)\}', runtime)
        css_named = (
            {mat.group(1) for mat in re.finditer(r'(\d+):"', css_named_match.group(1))}
            if css_named_match else set()
        )
        missing_css = [
            cid for cid in sorted(css_ids - css_named, key=int)
            if not (css_dir / f"{cid}.css").exists()
        ]

    # ── Static files ──────────────────────────────────────────────────────
    static_files = ["images/atlas.png"]
    missing_static = [s for s in static_files if not (model_dir / s).exists()]

    total = len(missing_js) + len(missing_css) + len(missing_static)
    if total == 0:
        print("  All JS/CSS chunks and static files present.")
        return

    print(f"  Fetching missing files: {len(missing_js)} JS, {len(missing_css)} CSS, {len(missing_static)} static …")
    ok = fail = 0

    def _download(url: str, dest: pathlib.Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                r = _curl_requests.get(url, impersonate="chrome", timeout=30)
                if r.status_code != 200:
                    return False
                dest.write_bytes(r.content)
                return True
            except Exception:
                if dest.exists():
                    dest.unlink()
                if attempt < 2:
                    time.sleep(0.5)
        return False

    for cid in missing_js:
        if _download(f"{cdn_base}/js/{cid}.js", js_dir / f"{cid}.js"):
            ok += 1
        else:
            fail += 1

    for cid in missing_css:
        css_dir.mkdir(parents=True, exist_ok=True)
        if _download(f"{cdn_base}/css/{cid}.css", css_dir / f"{cid}.css"):
            ok += 1
        else:
            fail += 1

    for rel in missing_static:
        dest = model_dir / rel
        if _download(f"{cdn_base}/{rel}", dest):
            ok += 1
        else:
            fail += 1

    print(f"  Downloaded: {ok}  Unavailable: {fail}")


# ── Staging ───────────────────────────────────────────────────────────────────

def build_staging(model_id: str) -> pathlib.Path:
    src = DOWNLOADS_DIR / model_id
    dst = STAGING_BASE / model_id

    if not src.exists():
        sys.exit(f"Error: model folder not found: {src}")

    # Backfill any missing lazy-loaded chunks before staging
    print("Checking for missing JS/CSS chunks …")
    _backfill_chunks(src)

    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    t0 = time.monotonic()
    copied = 0
    for f in src.rglob("*"):
        if f.is_dir():
            continue
        if f.name in SKIP_NAMES:
            continue

        is_modified = ".modified." in f.name

        if is_modified:
            # This IS a .modified. file — upload it with the plain name
            dest_name = f.name.replace(".modified.", ".")
        else:
            # Skip originals that have a .modified. counterpart
            mod_sibling = f.parent / (f.stem + ".modified" + f.suffix)
            if mod_sibling.exists():
                continue
            dest_name = f.name

        dest_rel = f.parent.relative_to(src) / dest_name
        dest_file = dst / dest_rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        if dest_rel == pathlib.Path("index.html"):
            _write_patched_index(f, dest_file, model_id)
        elif dest_name.startswith("graph_") and dest_name.endswith(".json"):
            _write_fixed_json(f, dest_file)
        elif (dest_rel.parent == pathlib.Path("api/player/models") / model_id
              and dest_name.startswith("files")):
            _write_fixed_files_manifest(f, dest_file)
        else:
            shutil.copy2(f, dest_file)

        copied += 1
        if copied % 200 == 0:
            elapsed = time.monotonic() - t0
            print(f"\r  Staging: {copied} files ({elapsed:.0f}s)…", end="", flush=True)

    elapsed = time.monotonic() - t0
    print(f"\rStaging ready: {dst}  ({copied} files, {elapsed:.0f}s)")
    return dst


def _write_fixed_json(src_file: pathlib.Path, dest_file: pathlib.Path) -> None:
    """Copy a graph JSON file, fixing expired validUntil dates."""
    text = src_file.read_text(encoding="utf-8")
    fixed, n = _fix_valid_until(text)
    if n:
        print(f"  {dest_file.name}: fixed {n} validUntil dates")
    dest_file.write_text(fixed, encoding="utf-8")


def _write_fixed_files_manifest(src_file: pathlib.Path, dest_file: pathlib.Path) -> None:
    """Copy a files manifest, fixing the expires epoch timestamp."""
    text = src_file.read_text(encoding="utf-8")
    fixed, n = _fix_expires(text)
    if n:
        print(f"  {dest_file.name}: fixed {n} expires timestamps")
    dest_file.write_text(fixed, encoding="utf-8")


def _write_patched_index(src_file: pathlib.Path, dest_file: pathlib.Path, model_id: str) -> None:
    """Patch index.modified.html for subfolder static hosting and save as index.html."""
    text = src_file.read_text(encoding="utf-8")

    # Insert <base href> so relative URLs resolve from the model subfolder
    # even when the URL lacks a trailing slash (e.g. /nXa2VtHUZYa?m=...).
    base_tag = f'<base href="/{model_id}/">'
    text = text.replace("<head>", f"<head>{base_tag}", 1)

    # Inject shim immediately after JSNetProxy so it wraps _replaceHost
    # and fetch before the showcase JS runs (execution order matters).
    jsnetproxy_tag = "<script blocking='render' src='JSNetProxy.js'></script>"
    if jsnetproxy_tag in text:
        text = text.replace(jsnetproxy_tag, jsnetproxy_tag + POST_INTERCEPTOR)
    else:
        # Fallback: inject before </head>
        text = text.replace("</head>", POST_INTERCEPTOR + "</head>", 1)

    # Fix expired validUntil dates in embedded MP_PREFETCHED_MODELDATA
    text, n_valid = _fix_valid_until(text)
    if n_valid:
        print(f"  index.html: fixed {n_valid} validUntil dates")

    dest_file.write_text(text, encoding="utf-8")


# ── Azure helpers ─────────────────────────────────────────────────────────────

# Set by main() from CLI args.
_SUBSCRIPTION: str = ""
_TENANT: str = ""


def az(*args, capture: bool = True, subscription: str = ""):
    cmd = [_AZ_EXE] + list(args)
    sub = subscription or _SUBSCRIPTION
    if sub:
        cmd += ["--subscription", sub]
    return subprocess.run(cmd, capture_output=capture, text=True)


def _has_tenant_session(tenant_id: str) -> bool:
    """Return True if the CLI already has a valid session for the given tenant."""
    r = az("account", "list", "--query", f"[?tenantId=='{tenant_id}'].id", "-o", "tsv")
    return r.returncode == 0 and bool(r.stdout.strip())


def ensure_logged_in() -> None:
    """Login to Azure CLI only if no valid session exists for the target tenant."""
    need_login = False
    if _TENANT:
        if _has_tenant_session(_TENANT):
            print(f"Azure: existing session found for tenant {_TENANT}")
        else:
            need_login = True
            print(f"No active session for tenant {_TENANT} — starting device-code login…")
            print("Open https://microsoft.com/devicelogin and enter the code shown.\n")
            result = subprocess.run([_AZ_EXE, "login", "--use-device-code", "--tenant", _TENANT])
            if result.returncode != 0:
                sys.exit("Azure login failed.")
    elif az("account", "show").returncode != 0:
        need_login = True
        print("Not signed in to Azure CLI — starting device-code login…")
        print("Open https://microsoft.com/devicelogin and enter the code shown.\n")
        result = subprocess.run([_AZ_EXE, "login", "--use-device-code"])
        if result.returncode != 0:
            sys.exit("Azure login failed.")
    user = az("account", "show", "--query", "user.name", "-o", "tsv").stdout.strip()
    print(f"Azure: signed in as {user}")


def find_storage_subscription() -> str:
    """Search all visible subscriptions for STORAGE_ACCOUNT; return its subscription ID."""
    r = az("account", "list", "--query", "[].id", "-o", "tsv")
    if r.returncode != 0:
        return ""
    for sub_id in r.stdout.strip().splitlines():
        sub_id = sub_id.strip()
        if not sub_id:
            continue
        check = subprocess.run(
            [_AZ_EXE, "storage", "account", "show",
             "--name", STORAGE_ACCOUNT,
             "--subscription", sub_id,
             "--query", "name", "-o", "tsv"],
            capture_output=True, text=True,
        )
        if check.returncode == 0 and check.stdout.strip() == STORAGE_ACCOUNT:
            return sub_id
    return ""


def get_storage_key() -> str:
    # Auto-detect subscription if not specified explicitly.
    effective_sub = _SUBSCRIPTION
    if not effective_sub:
        print(f"Locating '{STORAGE_ACCOUNT}' across all visible subscriptions…")
        effective_sub = find_storage_subscription()
        if not effective_sub:
            sys.exit(
                f"Storage account '{STORAGE_ACCOUNT}' was not found in any visible subscription.\n"
                f"If it lives in a personal Azure tenant, re-run with:\n"
                f"  python prepare_for_azure.py <modelId> --tenant <tenant-id>\n"
                f"Your tenant ID is shown in the Azure Portal under Azure Active Directory."
            )
        print(f"Found in subscription: {effective_sub}")

    print("Retrieving storage account key…")
    r = az(
        "storage", "account", "keys", "list",
        "--account-name", STORAGE_ACCOUNT,
        "--query", "[0].value",
        "-o", "tsv",
        subscription=effective_sub,
    )
    if r.returncode != 0:
        sys.exit(f"Could not retrieve storage account key:\n{r.stderr}")
    return r.stdout.strip()


def get_static_website_url() -> str:
    r = az(
        "storage", "account", "show",
        "--name", STORAGE_ACCOUNT,
        "--query", "primaryEndpoints.web",
        "-o", "tsv",
    )
    return r.stdout.strip().rstrip("/")


# ── Upload ────────────────────────────────────────────────────────────────────

def upload(staging: pathlib.Path, model_id: str, key: str, pattern: str = "") -> str:
    destination = f"$web/{model_id}"
    if pattern:
        # Use rglob to find matching files (works across subdirectories).
        # az upload-batch --pattern uses fnmatch which does NOT match across
        # path separators, so we upload individual files instead.
        matches = [f for f in staging.rglob(pattern) if f.is_file()]
        file_count = len(matches)
        print(f"Uploading {file_count} file(s) matching '{pattern}' to {STORAGE_ACCOUNT}/{destination} …")
        t0 = time.monotonic()
        for f in matches:
            rel = f.relative_to(staging).as_posix()
            blob_name = f"{model_id}/{rel}"
            result = subprocess.run(
                [_AZ_EXE, "storage", "blob", "upload",
                 "--account-name", STORAGE_ACCOUNT,
                 "--account-key", key,
                 "--container-name", "$web",
                 "--file", str(f),
                 "--name", blob_name,
                 "--overwrite", "true",
                 "--output", "none"],
            )
            if result.returncode != 0:
                print(f"  WARN: failed to upload {rel}")
        elapsed = time.monotonic() - t0
    else:
        file_count = sum(1 for _ in staging.rglob("*") if _.is_file())
        print(f"Uploading {file_count} files to {STORAGE_ACCOUNT}/{destination} …")

        cmd = [
            _AZ_EXE, "storage", "blob", "upload-batch",
            "--account-name", STORAGE_ACCOUNT,
            "--account-key", key,
            "--destination", destination,
            "--source", str(staging),
            "--overwrite", "true",
            "--output", "none",
        ]
        t0 = time.monotonic()
        result = subprocess.run(cmd)
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            sys.exit("Upload failed.")

    print(f"Upload complete ({elapsed:.0f}s).")

    base_url = get_static_website_url()
    return f"{base_url}/{model_id}/"


# ── Entry ─────────────────────────────────────────────────────────────────────

def _arg(flag: str) -> str:
    """Return the value after 'flag' in sys.argv, or empty string."""
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def main() -> None:
    global _SUBSCRIPTION, _TENANT

    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        print(__doc__)
        sys.exit(1)

    model_id = sys.argv[1]
    stage_only      = "--stage-only" in sys.argv
    upload_pattern  = _arg("--upload-pattern")  # e.g. "index.html" to push only changed files
    _SUBSCRIPTION   = _arg("--subscription")
    _TENANT         = _arg("--tenant") or TENANT  # default: personal tenant hardcoded above

    print(f"Model: {model_id}")
    staging = build_staging(model_id)

    if stage_only:
        print("Staging complete. Skipping upload (--stage-only).")
        return

    ensure_logged_in()
    key = get_storage_key()
    url = upload(staging, model_id, key, pattern=upload_pattern)

    shutil.rmtree(staging)
    print(f"Staging folder cleaned up.")
    print(f"\nDone! View model at:\n{url}")


if __name__ == "__main__":
    main()
