#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Update checks and updates for a Refreshed install."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# py 3.12 mkstemp hangs on windows
tempfile.TMP_MAX = 20

REPOSITORY = "Refreshed-Mod/patcher"
# release signing key
UPDATE_PUBLIC_KEY = "41c12919b2433ac01af10a8b4c433ffa0f7330a7cc6a3fd97b585f9c2ff79043"
FORMAT = 1
# signature covers the release text
RELEASE_FORMAT = 2
PACKAGE = "refreshed-patcher.zip"
API = "https://api.github.com/repos/" + REPOSITORY
ACTIVE_KEY = UPDATE_PUBLIC_KEY
GITHUB_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
# build progress per step, measured
BUILD_STEPS = [("Recovering", 0.01, "Rebuilding the game"), ("Restoring", 0.10, "Rebuilding the game"), ("Converting", 0.10, "Rebuilding the game"),
               ("Importing the converted", 0.17, "Rebuilding the game"), ("Resaving", 0.28, "Rebuilding the game"),
               ("Rebasing", 0.38, "Rebuilding the game"), ("Verifying", 0.40, "Rebuilding the game"),
               ("Applying", 0.45, "Rebuilding the game"), ("Composing", 0.48, "Rebuilding the game"),
               ("Importing the patched", 0.50, "Rebuilding the game"),
               ("Rebuilding converted", 0.68, "Rebuilding the game"),
               ("Exporting", 0.87, "Rebuilding the game"),
               ("Adding", 0.92, "Rebuilding the game"), ("Starting the patched", 0.93, "Checking the new build"),
               ("Installing", 0.99, "Installing")]
DOWNLOAD_SHARE = 0.05
VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


class UpdateError(Exception):
    pass


# Ed25519, a github account alone can't push updates
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _point_add(a, b):
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c, d = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1: result = _point_add(result, point)
        point, scalar = _point_add(point, point), scalar >> 1
    return result


def _recover_x(y, sign):
    if y >= _P: return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0: return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P: x = x * _I % _P
    if (x * x - x2) % _P: return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def _compress(point):
    z = pow(point[2], _P - 2, _P)
    x, y = point[0] * z % _P, point[1] * z % _P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def _decompress(data):
    y = int.from_bytes(data, "little")
    x = _recover_x(y & ((1 << 255) - 1), y >> 255)
    return None if x is None else (x, y & ((1 << 255) - 1), 1, x * (y & ((1 << 255) - 1)) % _P)


def _hash_int(data): return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _expand_secret(secret):
    digest = hashlib.sha512(secret).digest()
    return (int.from_bytes(digest[:32], "little") & ((1 << 254) - 8)) | (1 << 254), digest[32:]


def public_key(secret): return _compress(_point_mul(_expand_secret(secret)[0], _G))


def sign(secret, message):
    scalar, prefix = _expand_secret(secret)
    public, r = public_key(secret), _hash_int(prefix + message) % _L
    point_r = _compress(_point_mul(r, _G))
    return point_r + ((r + _hash_int(point_r + public + message) % _L * scalar) % _L).to_bytes(32, "little")


def verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64: return False
    point_a, point_r = _decompress(public), _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if point_a is None or point_r is None or s >= _L: return False
    left = _point_mul(s, _G)
    right = _point_add(point_r, _point_mul(_hash_int(signature[:32] + public + message) % _L, point_a))
    return (left[0] * right[2] - right[0] * left[2]) % _P == 0 and (left[1] * right[2] - right[1] * left[2]) % _P == 0


def version_key(version):
    if not isinstance(version, str) or len(version) > 100: raise UpdateError("Invalid release version.")
    match = VERSION.fullmatch(version)
    if not match: raise UpdateError("Release versions must use x.y.z or x.y.z-alpha.1.")
    parts = match[4].split(".") if match[4] else []
    if any(not part or (part.isdigit() and len(part) > 1 and part.startswith("0")) for part in parts):
        raise UpdateError("Invalid prerelease version.")
    return (*map(int, match.group(1, 2, 3)), not bool(parts), tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts))


def atomic_json(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        destination = urllib.parse.urlsplit(url)
        if destination.scheme != "https": raise UpdateError("Refusing an insecure download redirect.")
        redirected = super().redirect_request(request, fp, code, message, headers, url)
        if redirected and urllib.parse.urlsplit(request.full_url).netloc != destination.netloc:
            redirected.remove_header("Authorization")
        return redirected


def use_source(repository, key):
    global API, ACTIVE_KEY
    API, ACTIVE_KEY = "https://api.github.com/repos/" + repository, key


def parse_source(text):
    text = str(text).strip()
    match = re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+/[^/\s]+?)(?:\.git)?(?:/.*)?", text) or re.fullmatch(r"([^/\s]+/[^/\s]+)", text)
    if not match or not GITHUB_NAME.fullmatch(match.group(1)): raise UpdateError("Paste a GitHub link like https://github.com/owner/repository.")
    return match.group(1)


# used by build_payload.py
def release_metadata(secret, version, commit, package):
    text = json.dumps({"format": RELEASE_FORMAT, "version": version, "commit": commit,
                       "files": {PACKAGE: {"size": len(package), "sha256": hashlib.sha256(package).hexdigest()}}})
    return json.dumps({"format": RELEASE_FORMAT, "release": text, "signature": sign(secret, text.encode()).hex(),
                       "key": public_key(secret).hex()}, indent=2) + "\n"


def fingerprint(key_hex): return " ".join(hashlib.sha256(bytes.fromhex(key_hex)).hexdigest()[i:i + 4].upper() for i in range(0, 16, 4))


# survives updates
def saved_source(install):
    path = Path(install["state"]) / "source.json"
    if not path.is_file(): return {"repository": REPOSITORY, "key": UPDATE_PUBLIC_KEY, "official": True}
    try: data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): raise UpdateError("The saved update source is unreadable. Reset it to official in Update Settings.") from None
    if not isinstance(data, dict) or not GITHUB_NAME.fullmatch(str(data.get("repository", ""))) or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("key", ""))):
        raise UpdateError("The saved update source is invalid. Reset it to official in Update Settings.")
    return {"repository": data["repository"], "key": data["key"], "official": False}


def inspect_source(text):
    repository = parse_source(text)
    if repository.casefold() == REPOSITORY.casefold(): return {"repository": REPOSITORY, "key": UPDATE_PUBLIC_KEY, "official": True}
    use_source(repository, None)
    releases = json.loads(request(API + "/releases?per_page=30", 4 << 20))
    for release in releases if isinstance(releases, list) else []:
        if not isinstance(release, dict) or release.get("draft") or not release.get("published_at"): continue
        try: version_key(release.get("tag_name"))
        except UpdateError: continue
        if not signed(release): continue
        key = str(release_document(release).get("key", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key): raise UpdateError("That source has an invalid signing key.")
        use_source(repository, key)
        release_manifest(release)
        return {"repository": repository, "key": key, "official": False, "version": release["tag_name"], "fingerprint": fingerprint(key)}
    raise UpdateError("That source has no signed Refreshed releases.")


def https_context():
    context = ssl.create_default_context()
    bundle = Path(__file__).with_name("cacert.pem")
    if bundle.is_file(): context.load_verify_locations(cafile=str(bundle))
    return context


def request(url, maximum, destination=None, progress=None):
    if not url.startswith(API + "/"): raise UpdateError("Unexpected release download URL.")
    headers = {"User-Agent": "WEBFISHING-Refreshed-Updater", "Accept": "application/octet-stream" if "/releases/assets/" in url else "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    # uncomment this if you need a tester token for a private repo
    # token = os.environ.get("REFRESHED_GITHUB_TOKEN")
    # if token: headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=https_context()), ReleaseRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            chunks, count, digest = [], 0, hashlib.sha256()
            file = open(destination, "wb") if destination else None
            try:
                while True:
                    block = response.read(min(1 << 20, maximum + 1 - count))
                    if not block: break
                    count += len(block)
                    if count > maximum: raise UpdateError("Release download is larger than expected.")
                    digest.update(block)
                    if file: file.write(block)
                    else: chunks.append(block)
                    if progress: progress(len(block))
            finally:
                if file: file.close()
            return (count, digest.hexdigest()) if destination else b"".join(chunks)
    except urllib.error.HTTPError as error:
        if error.code in (403, 429) and (error.headers or {}).get("x-ratelimit-remaining") == "0":
            try: reset = int(error.headers.get("x-ratelimit-reset", ""))
            except ValueError: reset = int(time.time()) + 3600
            minutes = max(1, int((reset - time.time()) // 60) + 1)
            raise UpdateError(f"GitHub's limit for update checks was reached. Try again in {minutes} minute{'s' if minutes != 1 else ''}.") from None
        if error.code in (401, 403, 404):
            raise UpdateError("Cannot access GitHub releases. GitHub may be rate limiting requests.") from None
        raise UpdateError(f"GitHub returned HTTP {error.code}.") from None
    except OSError:
        raise UpdateError("Cannot reach GitHub. Check your connection and try again.") from None


def asset(release, name):
    matches = [entry for entry in release.get("assets", []) if isinstance(entry, dict) and entry.get("name") == name]
    if len(matches) != 1: raise UpdateError(f"Release is missing a unique {name} asset.")
    entry = matches[0]
    if type(entry.get("id")) is not int or entry["id"] <= 0: raise UpdateError("Invalid release asset ID.")
    return entry


# skips releases with missing files
def signed(release):
    names = {entry.get("name") for entry in release.get("assets", []) if isinstance(entry, dict)}
    return {"update.json", PACKAGE} <= names


# 60 requests/hour without a token, so cache
CACHE = None
LIST_SECONDS, FRESH_SECONDS = 300, 60
fresh = False


def release_list():
    url = API + "/releases?per_page=100"
    path = CACHE / ("releases-" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".json") if CACHE else None
    try: saved = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
    except (OSError, ValueError): saved = {}
    if isinstance(saved, dict) and isinstance(saved.get("body"), str) and isinstance(saved.get("time"), (int, float)):
        if 0 <= time.time() - saved["time"] < (FRESH_SECONDS if fresh else LIST_SECONDS): return saved["body"]
    body = request(url, 4 << 20).decode("utf-8")
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {"time": time.time(), "body": body})
    return body


def published_releases(install):
    releases = json.loads(release_list())
    if not isinstance(releases, list): raise UpdateError("Unexpected GitHub release response.")
    found = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or not release.get("published_at"): continue
        if release.get("prerelease") and not install.get("prereleases", True): continue
        if type(release.get("id")) is not int or release["id"] <= 0 or not signed(release): continue
        try: found.append((version_key(release.get("tag_name")), release))
        except UpdateError: continue
    return [release for _, release in sorted(found, key=lambda item: item[0], reverse=True)]


def latest_release(install):
    current = version_key(install["version"])
    newer = [release for release in published_releases(install) if version_key(release["tag_name"]) > current]
    return newer[0] if newer else None


def release_document(release):
    entry = asset(release, "update.json")
    # asset IDs never change
    path = CACHE / f"update-{entry['id']}.json" if CACHE else None
    body = path.read_bytes() if path and path.is_file() else None
    if body is None or entry.get("digest") != "sha256:" + hashlib.sha256(body).hexdigest():
        body = request(API + f"/releases/assets/{entry['id']}", 64 << 10)
        if path and entry.get("digest") == "sha256:" + hashlib.sha256(body).hexdigest():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    if entry.get("digest") != "sha256:" + hashlib.sha256(body).hexdigest():
        raise UpdateError("Release metadata checksum does not match GitHub.")
    try: document = json.loads(body)
    except ValueError: document = None
    if not isinstance(document, dict) or document.get("format") != RELEASE_FORMAT: raise UpdateError("This release needs a newer updater.")
    return document


def release_manifest(release):
    document = release_document(release)
    text, signature = document.get("release"), document.get("signature")
    try: signature = bytes.fromhex(signature)
    except (TypeError, ValueError): raise UpdateError("Invalid release signature.") from None
    if not isinstance(text, str) or not ACTIVE_KEY or not verify(bytes.fromhex(ACTIVE_KEY), text.encode(), signature):
        raise UpdateError("The release signature is not valid, so it was not installed.")
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("format") != RELEASE_FORMAT: raise UpdateError("This release needs a newer updater.")
    if data.get("version") != release.get("tag_name", "").removeprefix("v"):
        raise UpdateError("Release tag and update version do not match.")
    if not re.fullmatch(r"[0-9a-f]{40}", str(data.get("commit", ""))): raise UpdateError("Invalid release commit.")
    info = data.get("files", {}).get(PACKAGE) if isinstance(data.get("files"), dict) else None
    if not isinstance(info, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", ""))):
        raise UpdateError(f"Release checksum is missing for {PACKAGE}.")
    if type(info.get("size")) is not int or not 0 < info["size"] <= 256 << 20: raise UpdateError(f"Invalid release size for {PACKAGE}.")
    asset(release, PACKAGE)
    return data


def download_release(release, manifest, folder, progress=None):
    expected, done = manifest["files"][PACKAGE], 0
    def count(size):
        nonlocal done
        done += size
        if progress: progress(done / expected["size"])
    result = request(API + f"/releases/assets/{asset(release, PACKAGE)['id']}", expected["size"], folder / PACKAGE, progress=count)
    if result != (expected["size"], expected["sha256"]): raise UpdateError(f"Download checksum mismatch: {PACKAGE}.")
    with zipfile.ZipFile(folder / PACKAGE) as package:
        for name in ("patcher.py", "updater.py", "payload.zip"): (folder / name).write_bytes(package.read(name))
        if "cacert.pem" in package.namelist(): (folder / "cacert.pem").write_bytes(package.read("cacert.pem"))  # pre alpha.19
    with zipfile.ZipFile(folder / "payload.zip") as payload:
        target = json.loads(payload.read("manifest.json"))
    if target.get("target_commit") != manifest["commit"] or target.get("release_version") != manifest["version"]:
        raise UpdateError("The payload does not match this release.")


def process_running(pid):
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 5: raise UpdateError("Cannot check whether the game has closed.")
            return False
        try:
            code = ctypes.c_uint32()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)): raise UpdateError("Cannot check the running game.")
            return code.value == 259
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: raise UpdateError("Cannot check whether the game has closed.") from None
    stat_path = Path(f"/proc/{pid}/stat")
    try: return stat_path.read_text().rsplit(")", 1)[1].strip().split()[0] != "Z"
    except FileNotFoundError: return False


def wait_for_game(pid, status, version):
    atomic_json(status, {"phase": "ready", "version": version, "progress": 0.98,
                         "message": f"Update ready. Quit the game to finish updating to {version}."})
    while process_running(pid): time.sleep(0.5)


def load_install(path):
    install = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(install, dict) or install.get("format") != FORMAT or not isinstance(install.get("repository"), str):
        raise UpdateError("Unsupported Refreshed installation metadata.")
    if install.get("system") not in ("linux", "windows"): raise UpdateError("Unsupported update platform.")
    version_key(install.get("version"))
    for key in ("output", "game", "state", "python"):
        if not isinstance(install.get(key), str) or not Path(install[key]).is_absolute(): raise UpdateError(f"Invalid installation path: {key}.")
    if Path(install["output"]).resolve() != path.parent.resolve(): raise UpdateError("Installation folder has moved. Run the patcher again to set it up.")
    if Path(install["state"]).resolve() != path.parent.with_name("." + path.parent.name + "-updater").resolve():
        raise UpdateError("Unexpected updater folder.")
    return install


# OS lock, no stale locks
def acquire_lock(lock):
    file = open(lock, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise UpdateError("An update is already running.") from None
    return file


def update_running(state):
    try: release_lock(acquire_lock(state / "update.lock"))
    except UpdateError: return True
    return False


def release_lock(file):
    try:
        if sys.platform == "win32":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    finally: file.close()


class Progress:
    def __init__(self, status): self.status, self.last = status, 0.0

    def __call__(self, message, share, force=False):
        if not force and time.monotonic() - self.last < 0.25: return
        self.last = time.monotonic()
        atomic_json(self.status, {"phase": "building", "message": message, "progress": round(min(max(share, 0.0), 1.0), 3)})


def record_release(install, release):
    path = Path(install["output"]) / "install.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update({"notes": str(release.get("body") or "")[:4000], "installed_at": time.strftime("%Y-%m-%d", time.gmtime())})
        atomic_json(path, data)
    except (OSError, ValueError): pass


def build_release(release, status, folder, game, output, cache=None, steam=False, steam_dir=None, before_install=None, title="Downloading the update", keep_build=False):
    manifest = release_manifest(release)
    reporter = Progress(status)
    reporter(title, 0)
    download_release(release, manifest, folder, lambda share: reporter(title, share * DOWNLOAD_SHARE))
    spec = importlib.util.spec_from_file_location("refreshed_update_patcher", folder / "patcher.py")
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    if getattr(patcher, "UPDATE_API", None) != FORMAT: raise UpdateError("The release uses an incompatible patcher.")
    started = time.monotonic()
    def progress(message):
        seconds = int(time.monotonic() - started)
        with open(status.parent / "build.log", "a", encoding="utf-8") as log: log.write(f"[{seconds // 60}:{seconds % 60:02d}] {message}\n")
        for prefix, share, text in BUILD_STEPS:
            if str(message).startswith(prefix): reporter(text, DOWNLOAD_SHARE + share * (1 - DOWNLOAD_SHARE), force=True)
    extra = {"keep_build": keep_build} if "keep_build" in patcher.patch.__code__.co_varnames else {}  # pre alpha.19
    patcher.patch(game, output, payload_path=folder / "payload.zip", force=True, cache=cache, steam=steam,
                  steam_dir=steam_dir, progress=progress, before_install=before_install, **extra)
    return patcher


def keep_build_setting(state):
    try: return json.loads((Path(state) / "settings.json").read_text(encoding="utf-8")).get("keep_build", True) is not False
    except (OSError, ValueError, AttributeError): return True


def update(install, release, status, pid):
    state = Path(install["state"])
    notice = state / "last-update.json"  # shown by the game next start
    quit = []
    lock = acquire_lock(state / "update.lock")
    try:
        with tempfile.TemporaryDirectory(prefix="release-", dir=state) as temp:
            try:
                build_release(release, status, Path(temp), install["game"], install["output"], cache=install.get("cache"),
                              keep_build=keep_build_setting(state), before_install=lambda: (wait_for_game(pid, status, release["tag_name"]), quit.append(True)))
            except Exception as error:
                if type(error).__name__ == "PatchError" and str(error).startswith(("Unsupported WEBFISHING version", "Game executable not found")):
                    raise UpdateError(f"WEBFISHING at {install['game']} was updated or moved, so the update can't be rebuilt. "
                                      "Your current build was kept. Run patcher.py again with the current webfishing.exe.") from None
                raise
            record_release(install, release)
            atomic_json(status, {"phase": "done", "message": "Update installed.", "progress": 1.0})
        notice.unlink(missing_ok=True)
    except Exception as error:
        if quit: atomic_json(notice, {"version": release["tag_name"], "message": str(error)[:1000]})
        raise
    finally:
        release_lock(lock)


# uncomment this if you need a tester token for a private repo, plus the tester_token calls below and the header in request()
# TOKEN_FILE = "github-token"
#
#
# def save_tester_token(state):
#     token = os.environ.get("REFRESHED_GITHUB_TOKEN", "").strip()
#     if not token or not Path(state).is_dir(): return
#     fd = os.open(Path(state) / TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
#     with os.fdopen(fd, "w", encoding="utf-8") as file: file.write(token)
#
#
# def load_tester_token(state):
#     path = Path(state) / TOKEN_FILE
#     if not os.environ.get("REFRESHED_GITHUB_TOKEN") and path.is_file():
#         os.environ["REFRESHED_GITHUB_TOKEN"] = path.read_text(encoding="utf-8").strip()


def prune_jobs(state, keep):
    jobs = Path(state) / "jobs"
    if not jobs.is_dir() or update_running(Path(state)): return
    for folder in jobs.iterdir():
        if folder != keep and folder.is_dir() and not folder.is_symlink() and time.time() - folder.stat().st_mtime > 3600:
            shutil.rmtree(folder, ignore_errors=True)


def installed_here(output):
    path = Path(output) / "install.json"
    try: return load_install(path)
    except (OSError, ValueError, UpdateError): raise UpdateError(f"{output} is not a Refreshed installation, so nothing was changed.") from None


def local_patcher(here):
    spec = importlib.util.spec_from_file_location("refreshed_setup_patcher", here / "patcher.py")
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)
    return patcher


def detect(here):
    found = {"steam": "", "game": ""}
    try:
        patcher = local_patcher(here)
        steam = patcher.find_steam(patcher.host_platform())
        found["steam"] = str(steam)
        libraries = patcher.read_vdf((steam / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8-sig"))
        folders = [steam] + [Path(entry[0]["path"][0]) for entry in libraries["libraryfolders"][0].values()
                             if isinstance(entry[0], dict) and "path" in entry[0]]
        for folder in folders:
            exe = folder / "steamapps/common/WEBFISHING/webfishing.exe"
            if exe.is_file():
                found["game"] = str(exe)
                break
    except Exception: pass
    return found


# GUI installer
def setup(argv=None):
    parser = argparse.ArgumentParser(prog="updater.py setup")
    parser.add_argument("action", choices=["detect", "install", "restore-steam", "uninstall"])
    parser.add_argument("--output", help="Refreshed folder")
    parser.add_argument("--status", required=True, help="JSON progress file")
    parser.add_argument("--game", help="webfishing.exe or its folder, for install")
    steam_options = parser.add_mutually_exclusive_group()
    steam_options.add_argument("--no-steam", action="store_true", help="don't set Steam's Play button to launch Refreshed")
    steam_options.add_argument("--manual-steam", action="store_true", help="show launch parameters to paste into Steam; don't change Steam settings")
    parser.add_argument("--steam-dir")
    parser.add_argument("--no-keep-build", action="store_true", help="don't keep build files for faster updates")
    args = parser.parse_args(argv)
    status = Path(args.status).absolute()
    here = Path(__file__).resolve().parent
    global CACHE
    CACHE = None  # installer always asks GitHub
    if args.action == "detect":
        atomic_json(status, {"phase": "detected", **detect(here)})
        return 0
    if not args.output: parser.error("--output is required")
    output = Path(args.output).expanduser().absolute()
    try:
        lock = acquire_lock(status.parent / "setup.lock")
        try:
            if args.action == "install":
                if not args.game: raise UpdateError("Choose the WEBFISHING folder first.")
                steam = not args.no_steam and not args.manual_steam
                if args.manual_steam:
                    setup_patcher = local_patcher(here)
                    setup_patcher.steam_launch_command(output, setup_patcher.host_platform())
                use_source(REPOSITORY, UPDATE_PUBLIC_KEY)
                releases = published_releases({"prereleases": True})
                if not releases: raise UpdateError("No Refreshed release is available yet.")
                if output.exists(): installed_here(output)
                with tempfile.TemporaryDirectory(prefix="release-", dir=status.parent) as temp:
                    patcher = build_release(releases[0], status, Path(temp), args.game, output, steam=steam,
                                            steam_dir=args.steam_dir, title="Downloading Refreshed", keep_build=not args.no_keep_build)
                launch_options = ""
                if steam:
                    launch_options = json.loads((output / "steam-launch.json").read_text(encoding="utf-8"))["installed_launch_options"]
                elif args.manual_steam:
                    launch_options = setup_patcher.manual_steam_launch(output, patcher.host_platform())
                record_release({"output": str(output)}, releases[0])
                state = output.with_name("." + output.name + "-updater")
                # save_tester_token(state)
                atomic_json(state / "settings.json", {"keep_build": not args.no_keep_build})
                atomic_json(status, {"phase": "done", "progress": 1.0, "version": releases[0]["tag_name"], "steam": steam, "launch_options": launch_options,
                                     "executable": str(output / patcher.GAME_FILE[patcher.host_platform()])})
                return 0
            install = installed_here(output)
            patcher = local_patcher(here)
            if (output / "steam-launch.json").is_file():
                atomic_json(status, {"phase": "building", "message": "Restoring Steam's Play button", "progress": 0.2})
                patcher.restore_steam(output, patcher.host_platform())
            if args.action == "uninstall":
                state = Path(install["state"])
                if state.is_dir() and update_running(state): raise UpdateError("An update is still running. Wait a minute and try again.")
                atomic_json(status, {"phase": "building", "message": "Removing Refreshed", "progress": 0.6})
                kept = [Path(install["cache"]) / "build"] if install.get("cache") else []
                for folder in [output, state, *kept]:
                    if folder.is_dir() and not folder.is_symlink(): patcher.shutil.rmtree(folder, onerror=patcher._force_remove)
            atomic_json(status, {"phase": "done", "progress": 1.0})
            return 0
        finally:
            release_lock(lock)
    except Exception as error:
        atomic_json(status, {"phase": "error", "message": str(error)[:1500]})
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", required=True)
    parser.add_argument("--status", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="look for a newer release")
    mode.add_argument("--list", action="store_true", help="list published releases")
    mode.add_argument("--source", help="inspect another release source (GitHub link)")
    mode.add_argument("--set-source", help="use this source (GitHub link, or 'official')")
    mode.add_argument("--release", type=int, help="install this release ID")
    parser.add_argument("--fingerprint", help="key fingerprint confirmed by the player, for --set-source")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--allow-older", action="store_true", help="the player confirmed installing an older version")
    parser.add_argument("--fresh", action="store_true", help="the player asked to check: reuse GitHub answers for a minute only")
    args = parser.parse_args(argv)
    status = Path(args.status).resolve()
    try:
        install = load_install(Path(args.install).resolve())
        os.chdir(install["state"])
        # load_tester_token(install["state"])
        jobs = Path(install["state"]).resolve() / "jobs"
        if jobs not in status.parents or not status.parent.is_dir(): raise UpdateError("Unexpected updater status path.")
        global CACHE, fresh
        CACHE, fresh = Path(install["state"]).resolve() / "cache", args.fresh
        prune_jobs(install["state"], status.parent)
    except (OSError, ValueError, UpdateError) as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        if (args.check or args.release) and update_running(Path(install["state"])):
            atomic_json(status, {"phase": "busy", "message": "An update is finishing in the background. Restart the game in a minute."})
            return 0
        if args.set_source and args.set_source.strip().casefold() == "official":
            (Path(install["state"]) / "source.json").unlink(missing_ok=True)
            atomic_json(status, {"phase": "source_saved", "repository": REPOSITORY, "official": True})
            return 0
        source = saved_source(install)
        use_source(source["repository"], source["key"])
        if args.check:
            release = latest_release(install)
            if release:
                release_manifest(release)
                atomic_json(status, {"phase": "available", "release_id": release["id"], "version": release["tag_name"],
                                     "notes": str(release.get("body") or "")[:2000]})
            else: atomic_json(status, {"phase": "current", "message": "You're up to date."})
        elif args.list:
            current = version_key(install["version"])
            atomic_json(status, {"phase": "versions", "repository": source["repository"], "official": source["official"], "releases": [
                {"id": release["id"], "version": release["tag_name"], "date": str(release.get("published_at", ""))[:10],
                 "notes": str(release.get("body") or "")[:2000], "prerelease": bool(release.get("prerelease")),
                 "relation": "newer" if version_key(release["tag_name"]) > current else "installed" if version_key(release["tag_name"]) == current else "older"}
                for release in published_releases(install)[:50]]})
        elif args.source:
            found = inspect_source(args.source)
            atomic_json(status, {"phase": "source", **{key: found.get(key, "") for key in ("repository", "official", "version", "fingerprint")}})
        elif args.set_source:
            path = Path(install["state"]) / "source.json"
            found = inspect_source(args.set_source)
            if found["official"]: path.unlink(missing_ok=True)
            elif found["fingerprint"] != args.fingerprint: raise UpdateError("The source's signing key changed. Check it again before switching.")
            else: atomic_json(path, {"repository": found["repository"], "key": found["key"]})
            atomic_json(status, {"phase": "source_saved", "repository": found["repository"], "official": found["official"]})
        else:
            if not args.release or not args.parent_pid or args.parent_pid <= 0: raise UpdateError("Update needs a release ID and the running game PID.")
            release = json.loads(request(API + f"/releases/{args.release}", 2 << 20))
            if not isinstance(release, dict) or release.get("draft") or not release.get("published_at"):
                raise UpdateError("The selected release is no longer available.")
            relation = version_key(release.get("tag_name")) > version_key(install["version"])
            if version_key(release.get("tag_name")) == version_key(install["version"]): raise UpdateError("This version is already installed.")
            if not relation and not args.allow_older: raise UpdateError("This release is not newer than the installed version.")
            update(install, release, status, args.parent_pid)
        return 0
    except Exception as error:
        message = str(error)[:1000]
        atomic_json(status, {"phase": "error", "message": message})
        return 1


if __name__ == "__main__":
    sys.exit(setup(sys.argv[2:]) if sys.argv[1:2] == ["setup"] else main())
