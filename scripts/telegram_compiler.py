import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8773407190:AAGuU55775YWpLdQHGfLzc0v3ofwrtu6ob0").strip()
ALLOWED_USER = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "1436203873").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_ID = os.environ.get("INPUT_TELEGRAM_FILE_ID", "").strip()
CHAT_ID = os.environ.get("INPUT_CHAT_ID", "").strip()
REQUESTER_ID = os.environ.get("INPUT_REQUESTER_ID", "").strip()
FILENAME = os.environ.get("INPUT_FILENAME", "project.zip").strip()

if not TOKEN or not ALLOWED_USER or not FILE_ID or not CHAT_ID or not REQUESTER_ID:
    raise SystemExit("Missing Telegram token, allowed user, or workflow inputs.")
if REQUESTER_ID != ALLOWED_USER:
    raise SystemExit("Unauthorized Telegram user ID.")
if not FILENAME.lower().endswith(".zip"):
    raise SystemExit("Please send a .zip project file.")

session = requests.Session()
session.headers.update({"User-Agent": "telegram-theos-actions-compiler/1.0"})


def tg(method, **kwargs):
    r = session.post(f"{API}/{method}", data=kwargs, timeout=45)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}")
    return data["result"]


def send(chat_id, text):
    # Telegram message limit is 4096 characters.
    tg("sendMessage", chat_id=chat_id, text=text[:3900])


def send_document(chat_id, path, caption):
    with path.open("rb") as f:
        r = session.post(f"{API}/sendDocument", data={"chat_id": chat_id, "caption": caption[:900]}, files={"document": (path.name, f, "application/octet-stream")}, timeout=90)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram could not send the compiled file.")
    return data["result"]


def safe_extract_zip(data: bytes, dest: Path):
    if len(data) > 45 * 1024 * 1024:
        raise ValueError("ZIP is too large (limit: 45 MB).")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = zf.infolist()
        if len(members) > 3000:
            raise ValueError("ZIP contains too many files.")
        total = sum(x.file_size for x in members)
        if total > 250 * 1024 * 1024:
            raise ValueError("Uncompressed project is too large (limit: 250 MB).")
        for item in members:
            p = Path(item.filename)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("ZIP contains an unsafe path.")
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("ZIP contains a symbolic link; please remove it and re-upload.")
        zf.extractall(dest)


def locate_project(root: Path) -> Path:
    candidates = [root] + [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    for p in candidates:
        if (p / "Makefile").is_file():
            return p
    raise ValueError("Could not find Makefile at ZIP root or one folder deep.")


def run(cmd, cwd, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout


def compile_project(zip_bytes: bytes, work: Path):
    source = work / "project"
    source.mkdir()
    safe_extract_zip(zip_bytes, source)
    project = locate_project(source)
    log = []

    # Install system dependencies and a fresh Theos checkout on the ephemeral runner.
    rc, out = run(["bash", "-lc", "sudo apt-get update && sudo apt-get install -y --no-install-recommends build-essential clang lld make perl git curl python3 dpkg-dev fakeroot libzstd-dev"], work, 600)
    log.append("=== Install dependencies ===\n" + out[-7000:])
    if rc:
        raise RuntimeError("Could not install Linux build dependencies.\n" + "\n".join(log)[-3000:])

    env = os.environ.copy()
    # Never expose Telegram/GitHub credentials to untrusted project Makefiles or compiler subprocesses.
    for secret_name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID", "GITHUB_TOKEN", "GH_PAT", "ACTIONS_RUNTIME_TOKEN"):
        env.pop(secret_name, None)
    env["THEOS"] = str(work / "theos")
    rc, out = run(["git", "clone", "--depth", "1", "https://github.com/theos/theos.git", env["THEOS"]], work, 180)
    log.append("=== Install Theos ===\n" + out[-5000:])
    if rc:
        raise RuntimeError("Could not install Theos.\n" + "\n".join(log)[-3000:])

    # Download standard SDK collection when available.
    sdk_path = Path(env["THEOS"]) / "sdks"
    rc, out = run(["git", "clone", "--depth", "1", "https://github.com/theos/sdks.git", str(sdk_path)], work, 180)
    log.append("=== Install SDKs ===\n" + out[-4000:])
    if rc:
        log.append("SDK repository clone failed; build may require a project-provided SDK.\n")

    env["PATH"] = f"{env['THEOS']}/bin:" + env.get("PATH", "")
    # Run project build. Uploaded Makefiles/source are executable build input; only use trusted projects.
    rc, out = subprocess.getstatusoutput("true")  # initialize no shell state
    proc = subprocess.run(["make", "package"], cwd=project, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    log.append("=== make package ===\n" + proc.stdout[-16000:])
    if proc.returncode:
        # Some projects can build a dylib without a packaging target.
        proc2 = subprocess.run(["make"], cwd=project, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        log.append("=== make fallback ===\n" + proc2.stdout[-8000:])

    dylibs = [p for p in project.rglob("*.dylib") if p.is_file() and ".git" not in p.parts]
    debs = list(project.rglob("*.deb"))
    for deb in debs:
        extracted = work / ("deb_" + str(len(list(work.glob("deb_*")))))
        extracted.mkdir(exist_ok=True)
        p = subprocess.run(["dpkg-deb", "-x", str(deb), str(extracted)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode == 0:
            dylibs.extend(x for x in extracted.rglob("*.dylib") if x.is_file())
    # De-duplicate and avoid returning arbitrary files outside the temporary workspace.
    unique = []
    seen = set()
    for p in dylibs:
        try:
            resolved = p.resolve()
            resolved.relative_to(work.resolve())
        except Exception:
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    if not unique:
        raise RuntimeError("Build completed but no .dylib was found.\n" + "\n".join(log)[-3000:])
    chosen = unique[0]
    output = work / re.sub(r"[^A-Za-z0-9_.-]", "_", chosen.name)
    shutil.copy2(chosen, output)
    names = ", ".join(p.name for p in unique[:10])
    return output, "\n".join(log), names


def download_telegram_file(file_id):
    info = tg("getFile", file_id=file_id)
    file_path = info.get("file_path", "")
    if not file_path or ".." in Path(file_path).parts:
        raise ValueError("Telegram returned an invalid file path.")
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    r = session.get(url, timeout=90)
    r.raise_for_status()
    if len(r.content) > 45 * 1024 * 1024:
        raise ValueError("ZIP is too large (limit: 45 MB).")
    return r.content


def main():
    send(CHAT_ID, "Project received by GitHub Actions. Preparing the Theos build; this may take several minutes.")
    try:
        archive = download_telegram_file(FILE_ID)
        with tempfile.TemporaryDirectory(prefix="theos-build-") as tmp:
            out, log, names = compile_project(archive, Path(tmp))
            send_document(CHAT_ID, out, f"Build succeeded: {out.name}\nDylibs found: {names}")
            print(log[-12000:])
    except Exception as e:
        error = str(e)
        print("Build failed: " + error[-12000:])
        try:
            send(CHAT_ID, "Build failed.\n" + error[-2800:])
        except Exception as send_error:
            print("Could not notify Telegram: " + str(send_error))
        raise


if __name__ == "__main__":
    main()
