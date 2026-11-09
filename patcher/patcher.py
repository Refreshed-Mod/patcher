#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""WEBFISHING Refreshed patcher, builds Refreshed from your own copy of WEBFISHING.

    python3 patcher.py --game "/path/to/webfishing.exe" --output "/path/to/Refreshed"
"""
import argparse
import base64
import concurrent.futures
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath

# py 3.12 mkstemp hangs on windows
tempfile.TMP_MAX = 20

FORMAT = 2
UPDATE_API = 1
GAME_SHA256 = "65ddad962c7c718743e1185dc9a86ba3513af8670e82ee6ac1d369b10b3de1f8"
GITHUB = "https://github.com"
TOOLS = {
    "linux": {
        "gdre": (GITHUB + "/GDRETools/gdsdecomp/releases/download/v2.7.0/GDRE_tools-v2.7.0-linux.zip",
                 "abb4c197fe517d6a46b67faf41fc76d9890c882661a55ca2d49ba605955dfacc", "gdre_tools.x86_64"),
        "godot": (GITHUB + "/godotengine/godot/releases/download/4.7.2-stable/Godot_v4.7.2-stable_linux.x86_64.zip",
                  "cadd3204e728a35d3f13adb7fd0d7902636b79f6b95c40c265eb73b6c35329e4", "Godot_v4.7.2-stable_linux.x86_64"),
    },
    "windows": {
        "gdre": (GITHUB + "/GDRETools/gdsdecomp/releases/download/v2.7.0/GDRE_tools-v2.7.0-windows.zip",
                 "92a8d5d3d5c0cca159fc6cc34ebdda2cf7e1b448035cdabe1a67f0981cbd0a2c", "gdre_tools.exe"),
        "godot": (GITHUB + "/godotengine/godot/releases/download/4.7.2-stable/Godot_v4.7.2-stable_win64.exe.zip",
                  "731980f9608d61333e5baf54a2ef17210acc7a538446c0cb9969f002aca1e953", "Godot_v4.7.2-stable_win64_console.exe"),
    },
}
# range requests, no 1.3 GB download
TEMPLATES = GITHUB + "/godotengine/godot/releases/download/4.7.2-stable/Godot_v4.7.2-stable_export_templates.tpz"
RUNTIME = {"linux": ("templates/linux_release.x86_64", "d9f79ab89b5ae369aeed11c6052d402e8218cd503bf85b4a235f9c30c46a7c63"),
           "windows": ("templates/windows_release_x86_64.exe", "d34d36f3be1a6c49c56525ae86469b92e4f417ddf0b43cf00dd80c385c4b0562")}
GAME_FILE = {"linux": "webfishing-refreshed.x86_64", "windows": "WEBFISHING Refreshed.exe"}
EXPORT_PLATFORM = {"linux": "Linux", "windows": "Windows Desktop"}
STEAM_LIBS = {
    "linux": ["addons/godotsteam/linux64/libgodotsteam.linux.template_release.x86_64.so", "addons/godotsteam/linux64/libsteam_api.so"],
    "windows": ["addons/godotsteam/win64/libgodotsteam.windows.template_release.x86_64.dll", "addons/godotsteam/win64/steam_api64.dll"],
}
# silent import = hung
IMPORT_TIMEOUT, IMPORT_SILENCE = 600, 120
TEXT_TREES = ("conv", "resave", "resave_dec", "gen")
MEDIA_SUFFIXES = (".so", ".dll", ".png", ".ogg", ".wav", ".mp3", ".glb", ".stex", ".oggstr", ".mp3str", ".sample", ".jpg",
                  ".ttf", ".otf", ".ico", ".md5", ".gdc", ".res", ".scn", ".mesh")
INTERMEDIATES_FORMAT = 1  # bump on changes
# NixOS: steam-run
WRAPPER = os.environ.get("WEBFISHING_PATCHER_WRAPPER", "").split()


class PatchError(Exception): pass


def host_platform():
    if sys.platform.startswith("win"): name = "windows"
    elif sys.platform.startswith("linux"): name = "linux"
    else: raise PatchError("Unsupported operating system: only Windows 10/11 and Linux are supported.")
    if not re.fullmatch(r"(?i)x86_64|amd64", __import__("platform").machine()):
        raise PatchError("Unsupported CPU: a 64-bit x86 (x86_64) system is required.")
    return name


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""): digest.update(block)
    return digest.hexdigest()


def sha256(data): return hashlib.sha256(data).hexdigest()



def user_data_dir(system):
    if system == "windows": return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def default_cache_dir(system):
    if system == "windows":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "WEBFISHING-Refreshed-Patcher" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "webfishing-refreshed-patcher"


def protected_dirs(game_exe, system):
    data = user_data_dir(system)
    dirs = [game_exe.parent, data / "Godot" / "app_userdata" / "webfishing_2_newver", data / "webfishing-godot4"]
    steamapps = game_exe.parent.parent.parent
    if steamapps.name.lower() == "steamapps": dirs.append(steamapps / "compatdata" / "3146520")
    return dirs


def _resolved(path): return Path(os.path.normcase(os.path.realpath(path)))


def overlaps(a, b):
    a, b = _resolved(a), _resolved(b)
    return a == b or a in b.parents or b in a.parents


def check_game(game):
    path = Path(game).expanduser()
    if path.is_dir(): path = path / "webfishing.exe"
    if not path.is_file(): raise PatchError(f"Game executable not found: {path}")
    digest = sha256_file(path)
    if digest != GAME_SHA256:
        raise PatchError(f"Unsupported WEBFISHING version (SHA-256 {digest}).\n"
                         f"Only the release with SHA-256 {GAME_SHA256} is supported. Verify the game files in Steam and retry.")
    return path


def check_output(game_exe, output, system, force=False):
    output = Path(output).expanduser().absolute()
    for protected in protected_dirs(game_exe, system):
        if overlaps(output, protected):
            raise PatchError(f"Output {output} overlaps protected folder {protected}. Choose a separate folder.")
    if output.exists() and not force:
        raise PatchError(f"Output already exists: {output}\nChoose a new folder, or pass --force to replace it after a successful build.")
    if output.exists() and not output.is_dir(): raise PatchError(f"Output exists and is not a folder: {output}")
    if not output.parent.is_dir(): raise PatchError(f"Parent folder does not exist: {output.parent}")
    return output



def read_vdf(text):
    tokens = [(m.group(), m.start(), m.end()) for m in re.finditer(
        r'//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+', text)
        if not m.group().startswith("//")]
    cursor = 0

    def section(nested=False, depth=0):
        nonlocal cursor
        if depth > 128: raise PatchError("Steam settings are nested too deeply.")
        entries = {}
        while cursor < len(tokens):
            key, _, _ = tokens[cursor]
            if key == "}":
                if not nested: raise PatchError("Unexpected closing brace in Steam settings.")
                return entries
            if key == "{" or cursor + 1 == len(tokens): raise PatchError("Incomplete Steam settings.")
            key = re.sub(r'\\(["\\])', r'\1', key[1:-1]) if key.startswith('"') else key
            cursor += 1
            value, start, end = tokens[cursor]
            cursor += 1
            if value == "{":
                value = section(True, depth + 1)
                if cursor == len(tokens): raise PatchError("Unclosed section in Steam settings.")
                end = tokens[cursor][2]
                cursor += 1
            elif value == "}": raise PatchError("Missing value in Steam settings.")
            elif value.startswith('"'): value = re.sub(r'\\(["\\])', r'\1', value[1:-1])
            entries[key.casefold()] = (value, start, end)
        return entries

    return section()


STEAM_LAUNCH_PATH = ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", "3146520", "LaunchOptions")

def steam_launch_options(text, replacement=None):
    node = (read_vdf(text), 0, len(text))
    for index, key in enumerate(STEAM_LAUNCH_PATH):
        if not isinstance(node[0], dict): raise PatchError("Unexpected section type in Steam settings.")
        child = node[0].get(key.casefold())
        if child is None:
            if replacement is None: return None
            if index == 0: raise PatchError("Steam settings have no UserLocalConfigStore section.")
            close = node[2] - 1
            line_start = text.rfind("\n", 0, close) + 1
            indent = text[line_start:close]
            if indent.strip(): line_start, indent = close, ""
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = []
            for offset, name in enumerate(STEAM_LAUNCH_PATH[index:]):
                prefix = indent + "\t" * (offset + 1)
                if name == "LaunchOptions": lines.append(prefix + '"LaunchOptions"\t\t' + _vdf_quote(replacement))
                else: lines.extend((prefix + _vdf_quote(name), prefix + "{"))
            for offset in reversed(range(len(STEAM_LAUNCH_PATH) - index - 1)):
                lines.append(indent + "\t" * (offset + 1) + "}")
            return text[:line_start] + newline.join(lines) + newline + text[line_start:]
        node = child
    if not isinstance(node[0], str): raise PatchError("Steam launch options are not a string.")
    if replacement is None: return node[0]
    return text[:node[1]] + _vdf_quote(replacement) + text[node[2]:]


def _vdf_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def steam_is_running(system):
    if system == "windows":
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/FO", "CSV", "/NH"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return b'"steam.exe"' in result.stdout.lower()
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if process.stat().st_uid == os.getuid() and (process / "comm").read_text().strip() == "steam": return True
        except (OSError, ProcessLookupError): pass
    return False


def check_steam_closed(system):
    try: running = steam_is_running(system)
    except (OSError, subprocess.SubprocessError) as error:
        raise PatchError(f"Cannot check whether Steam is closed: {error}") from error
    if running: raise PatchError("Exit Steam before patching so it cannot overwrite the launch setting, or use --no-steam to only build a separate copy.")


def find_steam(system, steam_dir=None):
    if steam_dir: candidates = [Path(steam_dir).expanduser()]
    elif system == "windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                candidates = [Path(winreg.QueryValueEx(key, "SteamPath")[0])]
        except OSError: candidates = [Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"]
    else:
        candidates = [Path.home() / path for path in (".steam/steam", ".local/share/Steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam")]
    for candidate in candidates:
        if (candidate / "config/loginusers.vdf").is_file(): return candidate.resolve()
    raise PatchError("Steam installation not found. Use --steam-dir to select it, or --no-steam to only build a separate copy.")


SHELL_WORD = re.compile(r"""((?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s'"\\])+)\s*""")


# keeps other options as typed
def without_refreshed_launchers(options):
    try: shlex.split(options or "")
    except ValueError: return options
    kept, end = [], 0
    for word in SHELL_WORD.finditer(options or ""):
        if shlex.split(word.group(1))[0].endswith("/launch-refreshed.sh"):
            kept.append(options[end:word.start()])
            end = word.end()
    return "".join(kept) + options[end:] if kept else options


def steam_launch_command(output, system):
    if any(char in str(output) for char in ("\n", "\r", "%command%")):
        raise PatchError("The output path cannot contain newlines or %command% when configuring Steam.")
    if system == "windows": return '"' + str(output / GAME_FILE[system]) + '" -- %command%'
    return shlex.quote(str(output / "launch-refreshed.sh")) + " %command%"


def prepare_steam(output, system, steam_dir=None):
    check_steam_closed(system)
    root = find_steam(system, steam_dir)
    try: profiles = read_vdf((root / "config/loginusers.vdf").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as error: raise PatchError(f"Cannot read Steam accounts: {error}") from error
    users = profiles.get("users", ({}, 0, 0))[0]
    if not isinstance(users, dict): raise PatchError("Unexpected Steam account settings.")
    choices = []
    for steam_id, (fields, _, _) in users.items():
        if not steam_id.isdigit() or not isinstance(fields, dict): continue
        recent = fields.get("mostrecent", ("0", 0, 0))[0] == "1"
        timestamp = fields.get("timestamp", ("0", 0, 0))[0]
        choices.append((recent, int(timestamp) if str(timestamp).isdigit() else 0, int(steam_id) & 0xffffffff))
    if not choices: raise PatchError("No Steam accounts found. Sign into Steam once, then exit it and retry.")
    profile = max(choices)[2]
    config = root / "userdata" / str(profile) / "config/localconfig.vdf"
    try: original = config.read_bytes()
    except OSError as error: raise PatchError(f"Cannot read Steam settings: {config}\n{error}") from error
    try: text = original.decode("utf-8-sig")
    except UnicodeError as error: raise PatchError("Steam settings are not valid UTF-8.") from error
    previous = steam_launch_options(text)
    saved_path = output / "steam-launch.json"
    try: saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.is_file() else {}
    except (OSError, ValueError) as error: raise PatchError(f"Cannot read previous Steam launch settings: {saved_path}") from error
    if not isinstance(saved, dict): raise PatchError(f"Invalid previous Steam launch settings: {saved_path}")
    if saved.get("config") == str(config) and saved.get("installed_launch_options") == previous:
        if "previous_launch_options" not in saved or not isinstance(saved["previous_launch_options"], (str, type(None))):
            raise PatchError(f"Invalid previous Steam launch option: {saved_path}")
        previous = saved["previous_launch_options"]
    previous = without_refreshed_launchers(previous)
    options = steam_launch_command(output, system)
    if system == "linux" and previous and previous.count("%command%") == 1:
        options = previous.replace("%command%", options, 1)
    return {"config": str(config), "original": original, "updated": steam_launch_options(text, options),
            "previous_launch_options": previous, "installed_launch_options": options}


def write_steam_launch_files(out, plan, system):
    saved = {key: plan[key] for key in ("config", "previous_launch_options", "installed_launch_options")}
    (out / "steam-launch.json").write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    if system != "linux": return
    write_linux_launcher(out)


def write_linux_launcher(out):
    launcher = out / "launch-refreshed.sh"
    launcher.write_text('#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")"\n'
                        'if command -v steam-run >/dev/null 2>&1; then\n'
                        '    exec steam-run "$PWD/webfishing-refreshed.x86_64"\nfi\n'
                        'exec "$PWD/webfishing-refreshed.x86_64"\n', encoding="utf-8")
    launcher.chmod(0o755)


def manual_steam_launch(output, system):
    options = steam_launch_command(output, system)
    if system == "linux": write_linux_launcher(output)
    return options


def connect_steam(plan, output, system):
    check_steam_closed(system)
    if Path(plan["config"]).read_bytes() != plan["original"]:
        raise PatchError("Steam settings changed during patching. The separate build is ready, but its Steam launch setting was not changed. Exit Steam and retry.")
    write_steam_config(Path(plan["config"]), plan["original"], plan["updated"], system)


# only if Steam is closed
def write_steam_config(config, original, updated, system):
    fd, name = tempfile.mkstemp(prefix=".refreshed-launch-", dir=config.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write((b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        shutil.copymode(config, temp)
        check_steam_closed(system)
        if config.read_bytes() != original: raise PatchError("Steam settings changed; its launch setting was not updated.")
        os.replace(temp, config)
    finally:
        if temp.exists(): temp.unlink()


def restore_steam(output, system):
    record_path = Path(output).expanduser().absolute() / "steam-launch.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        config, installed, previous = Path(record["config"]), record["installed_launch_options"], record["previous_launch_options"]
    except (OSError, ValueError, KeyError, TypeError) as error: raise PatchError(f"No Steam launch backup found: {record_path}") from error
    check_steam_closed(system)
    try: original = config.read_bytes()
    except OSError as error: raise PatchError(f"Cannot read Steam settings: {config}") from error
    text = original.decode("utf-8-sig")
    if steam_launch_options(text) != installed:
        raise PatchError("WEBFISHING's Steam launch option was changed after Refreshed set it, so it was left unchanged.")
    write_steam_config(config, original, steam_launch_options(text, previous or ""), system)
    record_path.unlink()



def checked_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise PatchError("Invalid path in patch data.")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).drive or ".." in path.parts or path.as_posix() != name or name == ".":
        raise PatchError(f"Unsafe path in patch data: {name}")
    return name


def check_payload_paths(payload, manifest):
    for entry in payload.infolist():
        checked_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
    for entry in manifest["bases"]: checked_path(entry["name"])
    for entry in manifest["files"]:
        checked_path(entry["path"])
        for key in ("src", "blob"):
            if key in entry: checked_path(entry[key])
    for entry in manifest["licenses"]:
        checked_path(entry["to"])
        checked_path(entry["blob"])
    for name in manifest.get("meshes", {}):
        if len(PurePosixPath(checked_path(name)).parts) != 1: raise PatchError("Generated asset names must not contain folders.")

CA_BUNDLE = Path(__file__).with_name("cacert.pem")


# fresh windows lacks GitHub's root
def https_context():
    context = ssl.create_default_context()
    if CA_BUNDLE.is_file(): context.load_verify_locations(cafile=str(CA_BUNDLE))
    return context


def fetch_tool(kind, system, cache, progress):
    url, digest, exe = TOOLS[system][kind]
    folder = cache / f"{kind}-{digest[:12]}"
    if (folder / ".complete").is_file() and (folder / exe).is_file(): return folder / exe
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / url.rsplit("/", 1)[1]
    if not archive.is_file() or sha256_file(archive) != digest:
        progress(f"Downloading {archive.name}")
        part = archive.with_name(archive.name + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60, context=https_context()) as response, open(part, "wb") as file:
                shutil.copyfileobj(response, file, 1 << 20)
        except OSError as error:
            raise PatchError(f"Download failed: {url}\n{error}\nCheck your internet connection, or place the file in {cache}.") from error
        if sha256_file(part) != digest:
            part.unlink()
            raise PatchError(f"Checksum mismatch for {url}; the download was discarded. Retry later.")
        os.replace(part, archive)
    shutil.rmtree(folder, ignore_errors=True)
    extract_zip(archive, folder)
    if system == "linux":
        for item in folder.iterdir():
            if item.is_file(): item.chmod(item.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (folder / ".complete").write_text(digest)
    archive.unlink()
    return folder / exe


def fetch_range(url, start, end):
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(request, timeout=60, context=https_context()) as response:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if response.status != 206 or not match or int(match[1]) != start: raise PatchError(f"{url} does not support partial downloads.")
        return response.read(end - start + 1), int(match[3])


class RemoteFile(io.RawIOBase):
    def __init__(self, url):
        self.url, self.pos = url, 0
        self.size = fetch_range(url, 0, 0)[1]

    def readable(self): return True

    def seekable(self): return True

    def tell(self): return self.pos

    def seek(self, offset, whence=0):
        self.pos = (offset, self.pos + offset, self.size + offset)[whence]
        return self.pos

    def readinto(self, buffer):
        count = min(len(buffer), self.size - self.pos)
        if count <= 0: return 0
        data = fetch_range(self.url, self.pos, self.pos + count - 1)[0]
        if len(data) != count: raise PatchError(f"Incomplete download: {self.url}")
        buffer[:count] = data
        self.pos += count
        return count


def fetch_runtime(system, cache, progress):
    entry, digest = RUNTIME[system]
    path = cache / f"runtime-{digest[:12]}" / PurePosixPath(entry).name
    if path.is_file() and sha256_file(path) == digest: return path
    progress("Downloading the Godot runtime")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with zipfile.ZipFile(io.BufferedReader(RemoteFile(TEMPLATES), 8 << 20)) as archive, archive.open(entry) as source, open(part, "wb") as file:
            shutil.copyfileobj(source, file, 1 << 20)
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise PatchError(f"Download failed: {TEMPLATES}\n{error}\nCheck your internet connection and retry.") from error
    if sha256_file(part) != digest:
        part.unlink()
        raise PatchError("Checksum mismatch for the Godot runtime; the download was discarded. Retry later.")
    os.replace(part, path)
    return path


def extract_zip(archive, folder):
    with zipfile.ZipFile(archive) as zip_file:
        for item in zip_file.infolist():
            target = (folder / item.filename).resolve()
            if folder.resolve() not in target.parents: raise PatchError(f"Unsafe path in {archive}: {item.filename}")
        zip_file.extractall(folder)


# win 11 throttles windowless processes
def no_power_throttling(handle=-1):
    if os.name != "nt": return
    try:
        import ctypes
        class State(ctypes.Structure): _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
        call = ctypes.windll.kernel32.SetProcessInformation
        call.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        # older windows rejects the timer bit
        for mask in (1 | 4, 1):
            state = State(1, mask, 0)
            if call(handle, 4, ctypes.byref(state), ctypes.sizeof(state)): break  # 4 = ProcessPowerThrottling
    except (AttributeError, OSError): pass


def kill_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except OSError: pass
    process.kill()
    process.wait()


def run(cmd, log, env, cwd=None, marker=None, timeout=3600, silence=None):
    cmd = WRAPPER + [str(part) for part in cmd]
    with open(log, "ab") as out:
        out.write(("\n$ " + " ".join(cmd) + "\n").encode())
        out.flush()
        try:
            # kill the whole group if it hangs
            process = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console windows
                                       start_new_session=os.name != "nt")
        except FileNotFoundError as error: raise PatchError(f"Cannot start {cmd[0]}: {error}") from error
        no_power_throttling(getattr(process, "_handle", -1))
        started = quiet_since = time.monotonic()
        size = os.path.getsize(log)
        try:
            while True:
                try:
                    code = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired: pass
                now = time.monotonic()
                if os.path.getsize(log) != size: size, quiet_since = os.path.getsize(log), now
                if now - started > timeout or (silence and now - quiet_since > silence):
                    raise PatchError(f"{Path(cmd[len(WRAPPER)]).name} timed out; see {log}")
        finally:
            if process.poll() is None: kill_tree(process)
    text = Path(log).read_text(errors="replace")
    section = text[text.rfind("\n$ "):]
    if code != 0 or (marker and marker not in section) or "HELPER_FAIL" in section:
        tail = "\n".join(section.strip().splitlines()[-15:])
        raise PatchError(f"{Path(cmd[len(WRAPPER)]).name} failed (exit code {code}). Log: {log}\n{tail}")
    return section


# shared with the payload builder

B64 = re.compile(rb'PackedByteArray\("([A-Za-z0-9+/=]*)"\)')
DECL = re.compile(rb'\[(?:ext_resource|sub_resource)[^\]\n]*? id="([^"]+)"')
ARRAY = re.compile(rb'\b(?:Packed|Pool)\w*Array\(([^()"]*)\)')
BONE = re.compile(rb'bones/(\d+)/name = "([^"]*)"\s*\nbones/\1/parent = -?\d+\s*\nbones/\1/rest = Transform3D\(([^)]*)\)')
MULTIMESH = re.compile(rb'\ntransform_array = (?:Pool|Packed)Vector3Array\(([^)]*)\)')
TRACK = re.compile(rb'tracks/(\d+)/type = "transform"\s*\ntracks/\1/path = NodePath\("[^"]*:([^"]*)"\)'
                   rb'(?:\s*\ntracks/\1/[a-z_]+ = [^\n]*)*?\s*\ntracks/\1/keys = Packed\w+Array\(([^)]*)\)')


# godot 4 uses base64, the port uses decimal
def expand_byte_arrays(data):
    return B64.sub(lambda m: b"PackedByteArray(" + b", ".join(b"%d" % x for x in base64.b64decode(m.group(1))) + b")", data)


# reproducible resaves
def renumber_ids(data):
    ids = {}
    for match in DECL.finditer(data): ids.setdefault(match.group(1), b"%d" % len(ids))
    if not ids: return data
    pattern = re.compile(rb'(id="|Resource\(")(' + b"|".join(re.escape(k) for k in sorted(ids, key=len, reverse=True)) + rb')"')
    return pattern.sub(lambda m: m.group(1) + ids[m.group(2)] + b'"', data)


def bone_rests(text): return {m.group(2): [float(x) for x in m.group(3).split(b",")] for m in BONE.finditer(text)}


def _quat_from_rows(rows):
    (a, b, c), (d, e, f), (g, h, i) = rows
    if a + e + i > 0:
        s = math.sqrt(a + e + i + 1) * 2
        return ((h - f) / s, (c - g) / s, (d - b) / s, s / 4)
    if a > e and a > i:
        s = math.sqrt(1 + a - e - i) * 2
        return (s / 4, (b + d) / s, (c + g) / s, (h - f) / s)
    if e > i:
        s = math.sqrt(1 + e - a - i) * 2
        return ((b + d) / s, s / 4, (f + h) / s, (c - g) / s)
    s = math.sqrt(1 + i - a - e) * 2
    return ((c + g) / s, (f + h) / s, s / 4, (d - b) / s)


def _rows_from_quat(q):
    x, y, z, w = q
    s = 2 / (x * x + y * y + z * z + w * w)
    return [[1 - s * (y * y + z * z), s * (x * y - z * w), s * (x * z + y * w)],
            [s * (x * y + z * w), 1 - s * (x * x + z * z), s * (y * z - x * w)],
            [s * (x * z - y * w), s * (y * z + x * w), 1 - s * (x * x + y * y)]]


# godot 3 tracks are relative to rest
def rebase_bone_tracks(text, fallback_rests):
    rests = bone_rests(text) or fallback_rests
    out = []
    for match in TRACK.finditer(text):
        rest = rests.get(match.group(2))
        if rest is None: continue
        keys = [float(x) for x in match.group(3).split(b",")] if match.group(3).strip() else []
        rows, origin = [rest[0:3], rest[3:6], rest[6:9]], rest[9:12]
        position, rotation, scale = [], [], []
        for j in range(0, len(keys), 12):
            time_, transition, loc, quat = keys[j], keys[j + 1], keys[j + 2:j + 5], keys[j + 5:j + 9]
            position += [time_, transition] + [sum(rows[a][b] * loc[b] for b in range(3)) + origin[a] for a in range(3)]
            pose = _rows_from_quat(quat)
            rotation += [time_, transition] + list(_quat_from_rows([[sum(rows[a][k] * pose[k][b] for k in range(3)) for b in range(3)] for a in range(3)]))
            scale += [time_, transition] + keys[j + 9:j + 12]
        for values in (position, rotation, scale):
            out.append(b"PackedFloat32Array(" + b", ".join(repr(float(v)).encode() for v in values) + b")")
    return b"\n".join(out)


def multimesh_buffers(text):
    out = []
    for match in MULTIMESH.finditer(text):
        tokens = [t.strip() for t in match.group(1).split(b",")] if match.group(1).strip() else []
        buffer = []
        for i in range(0, len(tokens), 12):
            a, o = tokens[i:i + 9], tokens[i + 9:i + 12]
            buffer += a[0:3] + o[0:1] + a[3:6] + o[1:2] + a[6:9] + o[2:3]
        out.append(b"PackedFloat32Array(" + b", ".join(buffer) + b")")
    return out


def generated_arrays(text, fallback_rests):
    return b"\n".join(([rebase_bone_tracks(text, fallback_rests)] if b'type = "transform"' in text else []) + multimesh_buffers(text))


def numeric_arrays(data):
    spans = []
    for match in ARRAY.finditer(data):
        start, end = match.span(1)
        while start < end and data[start] in b" \t\r\n": start += 1
        while end > start and data[end - 1] in b" \t\r\n": end -= 1
        spans.append((start, end))
    return spans


def array_stats(body):
    try: values = [float(x) for x in body.split(b",")] if body.strip() else []
    except ValueError: return None
    return len(values), math.fsum(values), math.fsum(abs(v) for v in values)


def stats_match(a, b):
    return a is not None and b is not None and a[0] == b[0] and all(abs(x - y) <= 1e-3 * max(1.0, abs(b[2])) for x, y in zip(a[1:], b[1:]))


def _varint(data, pos):
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        shift += 7
        if byte < 128: return value, pos


def encode_varint(value):
    out = bytearray()
    while True:
        out.append((value & 127) | (128 if value > 127 else 0))
        value >>= 7
        if not value: return bytes(out)


# 0 literal, 1 copy, 2 generated
def apply_delta(ops, base):
    out, masked, pos, holes, spans_by_base = [], [], 0, [], {}
    while pos < len(ops):
        tag = ops[pos]
        pos += 1
        if tag == 0:
            size, pos = _varint(ops, pos)
            out.append(ops[pos:pos + size])
            masked.append(ops[pos:pos + size])
            pos += size
        elif tag == 1:
            index, pos = _varint(ops, pos)
            offset, pos = _varint(ops, pos)
            size, pos = _varint(ops, pos)
            data = base(index)
            if offset + size > len(data): raise PatchError("Patch data does not fit its source file.")
            out.append(data[offset:offset + size])
            masked.append(data[offset:offset + size])
        elif tag == 2:
            index, pos = _varint(ops, pos)
            ordinal, pos = _varint(ops, pos)
            count, pos = _varint(ops, pos)
            total, absolute = struct.unpack_from("<dd", ops, pos)
            pos += 16
            data = base(index)
            if index not in spans_by_base: spans_by_base[index] = numeric_arrays(data)
            spans = spans_by_base[index]
            if ordinal >= len(spans): raise PatchError("Generated array is missing from its source file.")
            body = data[spans[ordinal][0]:spans[ordinal][1]]
            holes.append(stats_match(array_stats(body), (count, total, absolute)))
            out.append(body)
            masked.append(b"\0")
        else: raise PatchError("Corrupt patch data.")
    return b"".join(out), b"".join(masked), holes



class Job:
    def __init__(self, system, staging, cache, progress):
        self.system, self.staging, self.cache, self.progress = system, staging, cache, progress
        self.logs = staging / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        home = staging / "home"
        self.env = dict(os.environ)
        for name, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("APPDATA", "data"), ("LOCALAPPDATA", "cache")):
            (home / sub).mkdir(parents=True, exist_ok=True)
            self.env[name] = str(home / sub)
        self.gdre = self.godot = self.runtime = None
        self._base_cache = {}

    def tool(self, *args, log, cwd=None, marker=None, timeout=3600, silence=None):
        return run(list(args), self.logs / log, self.env, cwd=cwd, marker=marker, timeout=timeout, silence=silence)

    def godot_run(self, project, *args, log, marker=None, timeout=3600):
        flags = ["--headless"]
        # skips the Steam autoload
        user = [] if "--script" not in args else ([] if "--" in args else ["--"]) + ["--validate-project"]
        command = [self.godot, *flags, "--path", project, *args, *user]
        if "--import" not in args: return self.tool(*command, log=log, marker=marker, timeout=timeout)
        # headless import hangs sometimes, retry once
        try: return self.tool(*command, log=log, marker=marker, timeout=IMPORT_TIMEOUT, silence=IMPORT_SILENCE)
        except PatchError as error:
            if "timed out" not in str(error): raise
            self.progress("Import stalled; retrying")
            return self.tool(*command, log=log, marker=marker, timeout=IMPORT_TIMEOUT)

    def base(self, name):
        if name not in self._base_cache: self._base_cache[name] = (self.staging / checked_path(name)).read_bytes()
        return self._base_cache[name]


def recover(job, game_exe):
    job.progress("Copying the game executable")
    source = job.staging / "source" / "webfishing.exe"
    source.parent.mkdir()
    shutil.copyfile(game_exe, source)
    if sha256_file(source) != GAME_SHA256: raise PatchError("Copied executable does not match; check the disk and retry.")
    job.progress("Recovering the original project with GDRETools (takes a minute)")
    rec = job.staging / "rec"
    log = job.tool(job.gdre, "--headless", f"--recover={source}", f"--output={rec}", log="gdre-recover.log")
    took = re.search(r"Recovery finished in (\S+)", log)
    if took: job.progress(f"GDRETools reports {took.group(1)}")
    if not (rec / "project.godot").is_file(): raise PatchError("Recovery did not produce a project; see logs/gdre-recover.log.")
    source.unlink()


def build_intermediates(job):
    rec, conv = job.staging / "rec", job.staging / "conv"
    job.progress("Converting the recovered project to Godot 4 text formats")
    shutil.copytree(rec, conv, ignore=lambda d, names: [n for n in names if n in (".import", ".autoconverted", "gdre_export.log")] if Path(d) == rec else [])
    by_dir = {}
    for path in sorted(conv.rglob("*.material")): by_dir.setdefault(path.parent, []).append(path)
    for folder, files in by_dir.items():
        out = job.staging / "bin2txt" / folder.relative_to(conv)
        job.tool(job.gdre, "--headless", *[f"--bin-to-txt={f}" for f in files], f"--output={out}", log="gdre-bin-to-txt.log")
        for f in files:
            converted = out / f.name
            if not converted.is_file(): raise PatchError(f"GDRETools did not convert {f.name}; see logs/gdre-bin-to-txt.log.")
            os.replace(converted, f.with_name(f.name + ".tres"))
    job.godot_run(conv, "--convert-3to4", log="convert-3to4.log", marker="Conversion ended")
    job.progress("Importing the converted project")
    job.godot_run(conv, "--import", log="convert-import.log")
    job.progress("Resaving converted scenes in Godot 4 format")
    resave = job.staging / "resave"
    job.godot_run(conv, "--script", job.staging / "helpers" / "resave.gd", "--", resave, log="resave.log", marker="HELPER_OK")
    for path in sorted(resave.rglob("*")):
        if not path.is_file(): continue
        data = renumber_ids(path.read_bytes())
        path.write_bytes(data)
        decimal = job.staging / "resave_dec" / path.relative_to(resave)
        decimal.parent.mkdir(parents=True, exist_ok=True)
        decimal.write_bytes(expand_byte_arrays(data))
    job.progress("Rebasing skeleton animations and instance buffers")
    fallback = bone_rests((conv / "Scenes/Entities/Player/player.tscn").read_bytes())
    for path in sorted(conv.rglob("*")):
        if path.suffix not in (".tscn", ".tres"): continue
        data = generated_arrays(path.read_bytes(), fallback)
        if not data: continue
        out = job.staging / "gen" / (path.relative_to(conv).as_posix() + ".txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)


def _intermediates_cache(job):
    key = sha256(json.dumps([INTERMEDIATES_FORMAT, GAME_SHA256, TOOLS[job.system]["gdre"][1], TOOLS[job.system]["godot"][1],
                             sha256_file(job.staging / "helpers" / "resave.gd")]).encode())
    return job.cache / f"intermediates-{key[:16]}.zip"


def _text_files(root, tree):
    for path in sorted((root / tree).rglob("*")):
        rel = path.relative_to(root / tree).as_posix()
        if path.is_file() and not rel.startswith(".") and not path.name.endswith(MEDIA_SUFFIXES): yield path


def restore_intermediates(job):
    archive = _intermediates_cache(job)
    if not archive.is_file(): return False
    job.progress("Restoring converted files from the cache")
    try:
        extract_zip(archive, job.staging)
        return True
    except (OSError, zipfile.BadZipFile, PatchError):
        discard_intermediates(job)
        return False


def discard_intermediates(job):
    for tree in TEXT_TREES: shutil.rmtree(job.staging / tree, ignore_errors=True)
    _intermediates_cache(job).unlink(missing_ok=True)


def save_intermediates(job):
    archive = _intermediates_cache(job)
    name = None
    try:
        job.cache.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".intermediates-", dir=job.cache)
        os.close(fd)
        with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
            for tree in TEXT_TREES:
                for path in _text_files(job.staging, tree): zip_file.write(path, path.relative_to(job.staging).as_posix())
        os.replace(name, archive)
        for old in job.cache.glob("intermediates-*.zip"):
            if old != archive: old.unlink()
    except OSError as error:
        job.progress(f"Could not save the conversion cache ({error}); the next update converts again.")
    finally:
        if name is not None:
            try: Path(name).unlink(missing_ok=True)
            except OSError: pass


KEPT_FORMAT = 1  # bump on changes


def _kept_key(job):
    return [KEPT_FORMAT, GAME_SHA256, TOOLS[job.system]["gdre"][1], TOOLS[job.system]["godot"][1]]


def restore_kept_build(job):
    kept = job.cache / "build"
    try: fresh = json.loads((kept / "key.json").read_text(encoding="utf-8")) == _kept_key(job)
    except (OSError, ValueError): fresh = False
    try:
        if fresh:
            job.progress("Reusing the files kept by the last build")
            (job.staging / "project").mkdir()
            for name in ("rec", "project/.godot"):
                if (kept / name).is_dir(): shutil.move(kept / name, job.staging / name)
    except OSError: fresh = False
    shutil.rmtree(kept, ignore_errors=True)
    return fresh and (job.staging / "rec" / "project.godot").is_file()


def save_kept_build(job):
    kept = job.cache / "build"
    try:
        shutil.rmtree(kept, ignore_errors=True)
        (kept / "project").mkdir(parents=True)
        for name in ("rec", "project/.godot"):
            if (job.staging / name).is_dir(): shutil.move(job.staging / name, kept / name)
        (kept / "key.json").write_text(json.dumps(_kept_key(job)), encoding="utf-8")  # last on purpose
    except OSError as error:
        shutil.rmtree(kept, ignore_errors=True)
        job.progress(f"Could not keep the build files ({error}); the next update rebuilds them.")


def verify_bases(job, manifest):
    job.progress("Verifying recovered and converted source files")
    bad = [b["name"] for b in manifest["bases"] if not (job.staging / b["name"]).is_file() or sha256_file(job.staging / b["name"]) != b["sha256"]]
    if bad:
        raise PatchError(f"{len(bad)} recovered/converted files differ from the expected versions, e.g. {bad[:5]}.\n"
                         "The recovery or a conversion tool produced different output; see the logs folder.")


def build_project(job, payload, manifest):
    job.progress(f"Applying the Refreshed patch ({len(manifest['files'])} files)")
    project = job.staging / "project"
    bases = [b["name"] for b in manifest["bases"]]
    for entry in manifest["files"]:
        target = project / checked_path(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        kind = entry["kind"]
        if kind == "copy": data = job.base(entry["src"])
        elif kind == "glb_image": data = glb_image(job.base(entry["src"]), entry["image"])
        elif kind == "blob": data = payload.read(entry["blob"])
        elif kind == "delta":
            data, masked, holes = apply_delta(payload.read(entry["blob"]), lambda i: job.base(bases[i]))
            if not all(holes): raise PatchError(f"Regenerated data for {entry['path']} does not match the expected values.")
            if holes and sha256(masked) != entry["masked_sha256"]: raise PatchError(f"Patch result mismatch: {entry['path']}")
        else: raise PatchError(f"Unknown patch entry type {kind}; update the patcher.")
        if "sha256" in entry and sha256(data) != entry["sha256"]: raise PatchError(f"Patch result mismatch: {entry['path']}")
        target.write_bytes(data)
        # content-based mtime so godot skips unchanged files
        stamp = 1_000_000_000 + int(sha256(data)[:7], 16)
        os.utime(target, (stamp, stamp))
    return project


def glb_image(data, index):
    size = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + size])
    binary = data[20 + size + 8:]
    view = gltf["bufferViews"][gltf["images"][index]["bufferView"]]
    start = view.get("byteOffset", 0)
    return binary[start:start + view["byteLength"]]


def write_png(path, width, height, rgba):
    raw = b"".join(b"\0" + bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def read_png(path):
    data = path.read_bytes()
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(data):
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        pos += 12 + size
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            if depth != 8 or color != 6 or body[12]: raise PatchError(f"{path.name} is not a non-interlaced RGBA8 PNG.")
        elif kind == b"IDAT": idat += body
    raw, stride, out, prev = zlib.decompress(idat), width * 4, bytearray(), bytearray(width * 4)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a, b, c = line[x - 4] if x >= 4 else 0, prev[x], prev[x - 4] if x >= 4 else 0
            if kind == 1: line[x] = (line[x] + a) & 255
            elif kind == 2: line[x] = (line[x] + b) & 255
            elif kind == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif kind == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    return width, height, out


def compose_logo(project, badges):
    width, height, logo = read_png(project / "Assets/Textures/UI/logo2.png")
    badge_width, badge_height, overlay = read_png(badges)
    if (width, height) != (480, 260) or (badge_width, badge_height) != (480, 260): raise PatchError("Unexpected logo images.")
    out = bytearray(width * height * 4)
    for y in range(217):
        start = ((y + 43) * width + 45) * 4
        out[y * width * 4:y * width * 4 + 435 * 4] = logo[start:start + 435 * 4]
    for i in range(0, len(out), 4):
        alpha = overlay[i + 3]
        if alpha == 255: out[i:i + 4] = overlay[i:i + 4]
        elif alpha:
            below = out[i + 3] * (255 - alpha) // 255
            total = alpha + below
            for c in range(3): out[i + c] = (overlay[i + c] * alpha + out[i + c] * below) // total
            out[i + 3] = total
    write_png(project / "Assets/Textures/UI/refreshedlogoalpha.png", width, height, out)


def generate_assets(job, project, manifest):
    helpers = job.staging / "helpers"
    job.progress("Composing the menu logo")
    compose_logo(project, helpers / "logo_badges.png")
    job.progress("Importing the patched project")
    job.godot_run(project, "--import", log="project-import.log")
    job.progress("Rebuilding converted meshes from the recovered models")
    expected = job.staging / "meshes.json"
    expected.write_text(json.dumps(manifest["meshes"]))
    job.godot_run(project, "--script", helpers / "meshes.gd", "--", "generate", expected, log="meshes.log", marker="HELPER_OK")
    job.godot_run(project, "--script", helpers / "mod_hashes.gd", log="mod-hashes.log", marker="HELPER_OK")


def export_game(job, project, payload, manifest):
    system = job.system
    job.progress("Exporting the game data")
    (project / "export_presets.cfg").write_text(
        '[preset.0]\n\nname="Refreshed"\nplatform="%s"\nrunnable=true\nexport_filter="all_resources"\n'
        'include_filter="mods/*/*.json, mods/*/package.sha256, modpacks/*/*.json"\nexclude_filter=""\nexport_path=""\n'
        'script_export_mode=2\n\n[preset.0.options]\n\nbinary_format/embed_pck=false\nbinary_format/architecture="x86_64"\n'
        'texture_format/s3tc_bptc=true\ntexture_format/etc2_astc=false\n' % EXPORT_PLATFORM[system])
    out = job.staging / "out"
    out.mkdir()
    name = GAME_FILE[system]
    pck = out / (Path(name).stem + ".pck")
    job.godot_run(project, "--export-pack", "Refreshed", pck, log="export.log")
    if not pck.is_file() or pck.stat().st_size < 50 << 20: raise PatchError("Export produced no game data; see logs/export.log.")
    job.progress("Adding the Godot runtime and Steam libraries")
    shutil.copyfile(job.runtime, out / name)
    for path in STEAM_LIBS[system]: (out / Path(path).name).write_bytes((project / path).read_bytes())
    for entry in manifest["licenses"]:
        target = out / "licenses" / entry["to"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload.read(entry["blob"]))
    if system == "linux": (out / name).chmod(0o755)
    return out


def smoke_test(job, out):
    job.progress("Starting the patched game headless to verify it loads")
    section = job.tool(out / GAME_FILE[job.system], "--headless", "--fixed-fps", "60", "--quit-after", "600", "--", "--validate-project", log="smoke.log", cwd=out, timeout=300)
    problems = [line for line in section.splitlines() if any(text in line for text in ("SCRIPT ERROR", "Parse Error", "Failed loading resource", "invalid UID"))]
    if problems or "Resoures Loaded" not in section:
        raise PatchError("The patched game did not start cleanly; see logs/smoke.log.\n" + "\n".join(problems[:10]))


MOVE_RETRY_SECONDS = 30


# Restart Manager
def folder_holders(folder):
    if os.name != "nt": return None
    try:
        import ctypes
        from ctypes import wintypes
        class Process(ctypes.Structure): _fields_ = [("pid", wintypes.DWORD), ("start", wintypes.FILETIME)]
        class Info(ctypes.Structure): _fields_ = [("process", Process), ("app", ctypes.c_wchar * 256), ("service", ctypes.c_wchar * 64),
                                                 ("type", ctypes.c_int), ("status", wintypes.ULONG), ("session", wintypes.DWORD), ("restartable", wintypes.BOOL)]
        rm = ctypes.WinDLL("rstrtmgr")
        session = wintypes.DWORD()
        if rm.RmStartSession(ctypes.byref(session), 0, ctypes.create_unicode_buffer(33)): return None
        try:
            files = [str(path) for path in Path(folder).rglob("*") if path.is_file()]
            for start in range(0, len(files), 500):
                batch = (ctypes.c_wchar_p * len(files[start:start + 500]))(*files[start:start + 500])
                if rm.RmRegisterResources(session, len(batch), batch, 0, None, 0, None): return None
            needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
            result = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
            if result == 234:  # ERROR_MORE_DATA
                infos = (Info * needed.value)()
                count.value = needed.value
                result = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
                if result: return None
                return [f"{info.app} (PID {info.process.pid})" for info in infos[:count.value]]
            return [] if result == 0 else None
        finally: rm.RmEndSession(session)
    except Exception: return None


def busy_note(folder):
    holders = folder_holders(folder)
    if holders is None: return ""
    if holders: return f" In use by: {', '.join(holders)}."
    return " No program has a file open there; an Explorer window or terminal inside the folder can also keep it busy."


# scanners and Steam hold it for a bit
def move_retrying(source, target, progress=None, label="The folder"):
    deadline = time.monotonic() + MOVE_RETRY_SECONDS
    noted = False
    while True:
        try: return os.replace(source, target)
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32) or time.monotonic() >= deadline: raise
            if progress and not noted: progress(f"{label} ({source}) is in use; retrying for {MOVE_RETRY_SECONDS} s.{busy_note(source)}")
            noted = True
            time.sleep(0.5)


def install(out, output, force=False, progress=None):
    old = None
    if output.exists():
        if not force: raise PatchError(f"Output already exists: {output}. Nothing was replaced.")
        old = output.with_name(f".{output.name}.old-{os.getpid()}")
        if old.exists(): raise PatchError(f"Previous install backup already exists: {old}. Nothing was replaced.")
        try: move_retrying(output, old, progress, "The installed game")
        except OSError as error:
            raise PatchError(f"Cannot replace {output}: {error}{busy_note(output)}\nClose the game and any program using that folder, then update again.") from error
    try: move_retrying(out, output, progress, "The new build")
    except BaseException as error:
        if old:
            try: move_retrying(old, output, progress, "The previous build")
            except OSError: raise PatchError(f"Cannot move the build into {output}, and putting the previous build back failed too. It is kept at {old}.") from error
        if isinstance(error, OSError): raise PatchError(f"Cannot move the build into {output}: {error}{busy_note(out)}") from error
        raise
    if old:
        # leftover backup isn't a failure
        try: shutil.rmtree(old, onerror=_force_remove)
        except OSError:
            if progress: progress(f"Could not remove the previous build at {old}")


LEFTOVER_SECONDS = 6 * 3600


# only stuff older than 6 h
def remove_leftovers(output, progress):
    if not output.parent.is_dir(): return
    for path in output.parent.iterdir():
        if not path.name.startswith((".refreshed-patch-", f".{output.name}.old-")) or path.is_symlink() or not path.is_dir(): continue
        try:
            if time.time() - path.stat().st_mtime < LEFTOVER_SECONDS: continue
            shutil.rmtree(path, onerror=_force_remove)
            progress(f"Removed leftover folder {path.name}")
        except OSError: progress(f"Could not remove leftover folder {path}")


def _force_remove(function, path, _info):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def update_repository(source):
    spec = importlib.util.spec_from_file_location("refreshed_updater_info", source)
    module = importlib.util.module_from_spec(spec)
    caching, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__
    try: spec.loader.exec_module(module)
    finally: sys.dont_write_bytecode = caching
    return module.REPOSITORY


def updater_files(out, output, game_exe, system, manifest, cache, repository):
    state = output.with_name("." + output.name + "-updater")
    if state.is_symlink(): raise PatchError(f"Updater folder cannot be a symlink: {state}")
    info = {"format": UPDATE_API, "repository": repository, "system": system,
            "version": manifest.get("release_version", "0.0.0+" + manifest["version"]),
            "commit": manifest.get("target_commit", manifest["version"]), "prereleases": True,
            "installed_at": time.strftime("%Y-%m-%d", time.gmtime()),
            "output": str(output), "game": str(game_exe.resolve()), "state": str(state),
            # MS Store python alias changes every update
            "python": str(Path(sys.executable).absolute()), "cache": str(cache.resolve())}
    (out / "install.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return state


def install_updater(state, source):
    state.mkdir(mode=0o700, exist_ok=True)
    (state / "jobs").mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".updater-", dir=state)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(source.read_bytes())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, state / "updater.py")
    finally:
        if temp.exists(): temp.unlink()
    if source.with_name(CA_BUNDLE.name).is_file(): shutil.copyfile(source.with_name(CA_BUNDLE.name), state / CA_BUNDLE.name)


def patch(game, output, payload_path=None, force=False, cache=None, keep_staging=False, progress=print, steam=True, steam_dir=None, before_install=None, keep_build=False):
    if sys.version_info < (3, 9): raise PatchError("Python 3.9 or newer is required.")
    no_power_throttling()
    system = host_platform()
    here = Path(__file__).resolve().parent
    payload_path = Path(payload_path) if payload_path else here / "payload.zip"
    if not payload_path.is_file(): raise PatchError(f"Patch data not found: {payload_path}")
    game_exe = check_game(game)
    output = check_output(game_exe, output, system, force)
    steam_plan = prepare_steam(output, system, steam_dir) if steam else None
    steam_record = steam_plan
    if not steam_record and (output / "steam-launch.json").is_file():
        try: steam_record = json.loads((output / "steam-launch.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error: raise PatchError("Cannot read the existing Steam launch backup.") from error
    updater_source = here / "updater.py"
    if not updater_source.is_file(): raise PatchError("Missing updater.py next to the patcher.")
    remove_leftovers(output, progress)
    staging = Path(tempfile.mkdtemp(prefix=".refreshed-patch-", dir=output.parent))
    started = time.time()
    installed = verified = False
    try:
        job = Job(system, staging, Path(cache) if cache else default_cache_dir(system), progress)
        with zipfile.ZipFile(payload_path) as payload:
            manifest = json.loads(payload.read("manifest.json"))
            if manifest.get("format") != FORMAT: raise PatchError("This payload needs a different patcher version.")
            if manifest["game_sha256"] != GAME_SHA256: raise PatchError("Payload is for a different game version.")
            check_payload_paths(payload, manifest)
            progress(f"Patch {manifest['version']} for WEBFISHING {GAME_SHA256[:12]}")
            (staging / "helpers").mkdir()
            for name in payload.namelist():
                if name.startswith("helpers/") and not name.endswith("/"): (staging / checked_path(name)).write_bytes(payload.read(name))
            job.gdre = fetch_tool("gdre", system, job.cache, progress)
            if not keep_build: shutil.rmtree(job.cache / "build", ignore_errors=True)
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                godot = pool.submit(lambda: (fetch_tool("godot", system, job.cache, progress), fetch_runtime(system, job.cache, progress)))
                if not (keep_build and restore_kept_build(job)):
                    for name in ("rec", "project"): shutil.rmtree(staging / name, ignore_errors=True)
                    recover(job, game_exe)
                job.godot, job.runtime = godot.result()
            converted = False
            if restore_intermediates(job):
                try: verify_bases(job, manifest)
                except PatchError:
                    job.progress("Cached conversion did not match; converting again")
                    discard_intermediates(job)
                    converted = True
            else: converted = True
            if converted:
                build_intermediates(job)
                verify_bases(job, manifest)
                save_intermediates(job)
            verified = True
            project = build_project(job, payload, manifest)
            generate_assets(job, project, manifest)
            out = export_game(job, project, payload, manifest)
        smoke_test(job, out)
        shutil.copytree(job.logs, out / "patch-logs")
        if steam_record: write_steam_launch_files(out, steam_record, system)
        elif system == "linux": write_linux_launcher(out)
        state = updater_files(out, output, game_exe, system, manifest, job.cache, update_repository(updater_source))
        if before_install: before_install()
        progress("Installing")
        install(out, output, force=force, progress=progress)
        installed = True
        install_updater(state, updater_source)
        if steam_plan:
            progress("Setting Steam's Play button to launch Refreshed")
            try: connect_steam(steam_plan, output, system)
            except (OSError, PatchError) as error:
                raise PatchError(f"Refreshed was built at {output}, but Steam setup failed: {error}") from error
        progress(f"Done in {int(time.time() - started)} s: {output / GAME_FILE[system]}")
        return output / GAME_FILE[system]
    except KeyboardInterrupt:
        message = f"Interrupted after installing the separate build at {output}. Steam setup may be incomplete." if installed else "Interrupted; nothing was installed."
        raise PatchError(message) from None
    finally:
        if keep_build and verified: save_kept_build(job)
        if keep_staging: progress(f"Staging kept: {staging}")
        else:
            try: shutil.rmtree(staging, onerror=_force_remove)
            except OSError: progress(f"Could not remove the temporary build folder {staging}; a later update removes it.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build WEBFISHING: Refreshed (Godot 4) from your own WEBFISHING installation.")
    parser.add_argument("--game", help="path to webfishing.exe (or its folder) from your Steam installation")
    parser.add_argument("--output", required=True, help="new folder for the Refreshed build")
    parser.add_argument("--force", action="store_true", help="replace an existing output folder after a successful build")
    parser.add_argument("--payload", help="patch data zip (default: payload.zip next to this script)")
    parser.add_argument("--cache", help="download cache folder")
    parser.add_argument("--keep-build", action="store_true", help="keep the recovered project and import cache (about 450 MB) so updates are faster")
    parser.add_argument("--keep-staging", action="store_true", help="keep temporary files for troubleshooting")
    parser.add_argument("--no-steam", action="store_true", help="only build a separate copy; don't set Steam's Play button to launch it")
    parser.add_argument("--restore-steam", action="store_true", help="restore Steam's previous WEBFISHING launch option for the build in --output (exit Steam first)")
    parser.add_argument("--steam-dir", help="Steam client folder, if it cannot be found automatically")
    args = parser.parse_args(argv)
    if not args.restore_steam and not args.game: parser.error("--game is required")
    try:
        if args.restore_steam:
            restore_steam(args.output, host_platform())
            print("Restored WEBFISHING's previous Steam launch option.")
        else: patch(args.game, args.output, args.payload, args.force, args.cache, args.keep_staging, steam=not args.no_steam, steam_dir=args.steam_dir, keep_build=args.keep_build)
    except PatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
