#!/usr/bin/env python3
"""python3 patcher/test_patcher.py, no game or Godot needed"""
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch as mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patcher  # noqa: E402
from patcher import PatchError  # noqa: E402


def raises(function, *args, text=""):
    try: function(*args)
    except PatchError as error:
        assert text in str(error), error
        return
    raise AssertionError(f"{function.__name__} did not fail")


def test_steam(root):
    steam = root / "Steam"
    (steam / "config").mkdir(parents=True)
    (steam / "config/loginusers.vdf").write_text(
        '"users" { "76561197960265729" { "MostRecent" "1" "Timestamp" "1" } '
        '"76561197960265730" { "MostRecent" "0" "Timestamp" "999" } }')
    config = steam / "userdata/1/config/localconfig.vdf"
    config.parent.mkdir(parents=True)
    text = ('"UserLocalConfigStore"\r\n{\r\n\t"Software"\r\n\t{\r\n\t\t"Valve"\r\n\t\t{\r\n'
            '\t\t\t"Steam"\r\n\t\t\t{\r\n\t\t\t\t"apps"\r\n\t\t\t\t{\r\n'
            '\t\t\t\t\t"3146520"\r\n\t\t\t\t\t{\r\n'
            '\t\t\t\t\t\t"LaunchOptions"\t"gamemoderun mangohud %command%"\r\n'
            '\t\t\t\t\t\t"Keep" "braces { } and \\"quoted\\" text"\r\n'
            '\t\t\t\t\t}\r\n\t\t\t\t\t"480" { "LaunchOptions" "leave me" }\r\n'
            '\t\t\t\t}\r\n\t\t\t}\r\n\t\t}\r\n\t}\r\n}\r\n')
    original = text.encode("utf-8")
    config.write_bytes(original)
    output = root / "Refreshed ' $ & (copy)"
    output.mkdir()
    with mock.object(patcher, "steam_is_running", return_value=False):
        plan = patcher.prepare_steam(output, "linux", steam)
        assert config.read_bytes() == original
        assert plan["previous_launch_options"] == 'gamemoderun mangohud %command%'
        assert plan["installed_launch_options"].startswith('gamemoderun mangohud ')
        assert patcher.steam_launch_options(plan["updated"]) == plan["installed_launch_options"]
        assert plan["updated"].replace(patcher._vdf_quote(plan["installed_launch_options"]), '"gamemoderun mangohud %command%"') == text
        patcher.write_steam_launch_files(output, plan, "linux")
        patcher.connect_steam(plan, output, "linux")
        saved = json.loads((output / "steam-launch.json").read_text())
        assert saved["previous_launch_options"] == 'gamemoderun mangohud %command%'
        assert "original" not in saved and '"Keep"' not in (output / "steam-launch.json").read_text()
        updated = config.read_bytes()
        assert b'"480" { "LaunchOptions" "leave me" }' in updated
        assert not list(config.parent.glob('.refreshed-launch-*'))

        # same build keeps the original setting
        repeat = patcher.prepare_steam(output, "linux", steam)
        assert repeat["previous_launch_options"] == plan["previous_launch_options"]
        assert repeat["installed_launch_options"] == plan["installed_launch_options"]

        # windows passes the old command after --
        windows = patcher.prepare_steam(root / "Windows build", "windows", steam)
        assert windows["installed_launch_options"] == '"' + str(root / "Windows build" / patcher.GAME_FILE["windows"]) + '" -- %command%'
        assert patcher.steam_launch_options(windows["updated"]) == windows["installed_launch_options"]

        # linux launcher needs posix paths
        if os.name != "nt":
            moved = patcher.prepare_steam(root / "Moved build", "linux", steam)
            assert moved["previous_launch_options"] == 'gamemoderun mangohud %command%'
            assert moved["installed_launch_options"] == "gamemoderun mangohud '" + str(root / "Moved build" / "launch-refreshed.sh") + "' %command%"
        # quoted vars stay assignments
        assert patcher.without_refreshed_launchers('FOO="a b" \'/old/it\'"\'"\'s/launch-refreshed.sh\' %command% -x') == 'FOO="a b" %command% -x'

        config.write_bytes(updated + b'\n// unrelated edit\n')
        raises(patcher.connect_steam, repeat, output, "linux", text="changed during patching")
        assert config.read_bytes().endswith(b'// unrelated edit\n')
        config.write_bytes(updated)
        with mock.object(patcher.os, "replace", side_effect=OSError("read only")):
            try: patcher.connect_steam(repeat, output, "linux")
            except OSError: pass
            else: raise AssertionError("Expected failed settings replacement")
        assert config.read_bytes() == updated
        assert not list(config.parent.glob('.refreshed-launch-*'))
    with mock.object(patcher, "steam_is_running", return_value=True):
        raises(patcher.prepare_steam, output, "linux", steam, text="Exit Steam")
        raises(patcher.connect_steam, plan, output, "linux", text="Exit Steam")
        assert config.read_bytes() == updated

    # no app settings yet
    missing = text[:text.index('\t\t\t\t\t"3146520"')] + text[text.index('\t\t\t\t\t"480"'):]
    assert patcher.steam_launch_options(missing) is None
    for sample in (missing, '"UserLocalConfigStore"\n{\n}\n'):
        inserted = patcher.steam_launch_options(sample, '"C:\\Games\\Refreshed.exe" -- %command%')
        assert patcher.steam_launch_options(inserted) == '"C:\\Games\\Refreshed.exe" -- %command%'
    raises(patcher.steam_launch_options, '"UserLocalConfigStore" { "missing"', text="Incomplete")

    # manual setup, Steam can run
    for system in ('linux', 'windows'):
        target = root / ('manual ' + system + " ' ü")
        target.mkdir()
        before = config.read_bytes()
        with mock.object(patcher, 'prepare_steam', side_effect=AssertionError('Manual setup must not read Steam settings')):
            options = patcher.manual_steam_launch(target, system)
        assert options == patcher.steam_launch_command(target, system)
        assert '%command%' in options and config.read_bytes() == before
        assert not (target / 'steam-launch.json').exists()
        assert (target / 'launch-refreshed.sh').exists() == (system == 'linux')
        raises(patcher.steam_launch_command, root / 'bad%command%path', system, text='output path')

    if sys.platform.startswith("linux"):
        bin_dir = root / "launcher-test-bin"
        bin_dir.mkdir()
        (bin_dir / "dirname").symlink_to(shutil.which("dirname"))
        binary = output / patcher.GAME_FILE["linux"]
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$#"\n')
        binary.chmod(0o755)
        args = [str(output / 'launch-refreshed.sh'), '/fake/proton', 'waitforexitandrun', '/fake/original.exe']
        env = {**os.environ, 'PATH': str(bin_dir)}
        direct = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
        assert direct.stdout.splitlines() == [str(output), '0']
        wrapper = bin_dir / 'steam-run'
        wrapper.write_text('#!/bin/sh\nprintf "steam-run\\n"\nexec "$@"\n')
        wrapper.chmod(0o755)
        wrapped = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
        assert wrapped.stdout.splitlines() == ['steam-run', str(output), '0']

    # default flow and opt out
    payload = root / "integration-payload.zip"
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({"format": patcher.FORMAT, "game_sha256": patcher.GAME_SHA256,
                         "version": "test", "bases": [], "files": [], "licenses": []}))
    original_exe = root / 'Original game/webfishing.exe'
    original_exe.parent.mkdir()
    original_exe.write_bytes(b'original game unchanged')
    for system in ('linux', 'windows'):
        def export(job, *args):
            out = job.staging / 'out'
            out.mkdir()
            (out / patcher.GAME_FILE[job.system]).write_text('test runtime')
            return out
        with ExitStack() as stack:
            stack.enter_context(mock.object(patcher, 'host_platform', return_value=system))
            stack.enter_context(mock.object(patcher, 'check_game', return_value=original_exe))
            stack.enter_context(mock.object(patcher, 'steam_is_running', return_value=False))
            stack.enter_context(mock.object(patcher, 'export_game', side_effect=export))
            for name in ('fetch_tool', 'fetch_runtime', 'recover', 'restore_intermediates', 'build_intermediates', 'verify_bases', 'save_intermediates', 'build_project', 'generate_assets', 'smoke_test'):
                stack.enter_context(mock.object(patcher, name))
            target = root / ('default ' + system)
            pristine = config.read_bytes()
            result = patcher.patch(original_exe, target, payload_path=payload, steam=True, steam_dir=steam, progress=lambda _: None)
            assert result == target / patcher.GAME_FILE[system]
            saved = json.loads((target / 'steam-launch.json').read_text())
            assert patcher.steam_launch_options(config.read_text(encoding='utf-8-sig')) == saved['installed_launch_options']
            assert (target / 'launch-refreshed.sh').exists() == (system == 'linux')
            info = json.loads((target / 'install.json').read_text())
            assert info['game'] == str(original_exe.resolve()) and info['repository'] == 'Refreshed-Mod/patcher'
            assert Path(info['state']).parent == target.parent
            assert (Path(info['state']) / 'updater.py').is_file() and (Path(info['state']) / 'cacert.pem').is_file()
            assert not list(root.glob('.refreshed-patch-*'))
            before = config.read_bytes()
            backup = (target / 'steam-launch.json').read_bytes()
            def reject_handoff():
                assert (target / patcher.GAME_FILE[system]).read_text() == 'test runtime'
                raise patcher.PatchError('handoff cancelled')
            raises(lambda: patcher.patch(original_exe, target, payload_path=payload, force=True, steam=False,
                   before_install=reject_handoff, progress=lambda _: None), text='handoff cancelled')
            assert (target / 'steam-launch.json').read_bytes() == backup
            handoffs = []
            patcher.patch(original_exe, target, payload_path=payload, force=True, steam=False,
                          before_install=lambda: handoffs.append(True), progress=lambda _: None)
            assert handoffs == [True] and config.read_bytes() == before
            if system == 'linux':
                # killed runs get cleaned after 6 h
                stale, fresh, backup_dir = root / '.refreshed-patch-stale', root / '.refreshed-patch-fresh', root / f'.{target.name}.old-1'
                for folder in (stale, fresh, backup_dir): (folder / 'sub').mkdir(parents=True)
                for folder in (stale, backup_dir): os.utime(folder, (time.time() - 7 * 3600,) * 2)
                rmtree, notes = shutil.rmtree, []
                def held(path, *args, **kwargs):
                    if Path(path).parent == root and Path(path).name.startswith('.refreshed-patch-') and Path(path) != stale: raise PermissionError('held')
                    return rmtree(path, *args, **kwargs)
                with mock.object(patcher.shutil, 'rmtree', side_effect=held):
                    patcher.patch(original_exe, target, payload_path=payload, force=True, steam=False, progress=notes.append)
                assert not stale.exists() and not backup_dir.exists() and fresh.exists()
                assert any('temporary build folder' in note for note in notes), notes
                for folder in root.glob('.refreshed-patch-*'): shutil.rmtree(folder)
            assert (target / 'steam-launch.json').read_bytes() == backup
            assert (target / 'launch-refreshed.sh').exists() == (system == 'linux')
            with mock.object(patcher, 'prepare_steam', side_effect=AssertionError('should not touch Steam')):
                standalone = root / ('standalone ' + system)
                patcher.patch(original_exe, standalone, payload_path=payload, steam=False, progress=lambda _: None)
                assert not (standalone / 'steam-launch.json').exists()
                assert (standalone / 'launch-refreshed.sh').exists() == (system == 'linux')
                assert config.read_bytes() == before
            assert original_exe.read_bytes() == b'original game unchanged'
            # --restore-steam
            edited = patcher.steam_launch_options(config.read_text(encoding='utf-8-sig'), 'user changed this')
            config.write_text(edited)
            raises(patcher.restore_steam, target, system, text='left unchanged')
            config.write_bytes(before)
            patcher.restore_steam(target, system)
            # restore drops old launchers
            assert patcher.steam_launch_options(config.read_text(encoding='utf-8-sig')) == (patcher.without_refreshed_launchers(patcher.steam_launch_options(pristine.decode('utf-8-sig'))) or '')
            assert not (target / 'steam-launch.json').exists()
            raises(patcher.restore_steam, target, system, text='No Steam launch backup')

    with mock.object(patcher, 'patch') as invoke:
        assert patcher.main(['--game', 'game.exe', '--output', 'out']) == 0
        assert invoke.call_args.kwargs['steam'] is True
        assert patcher.main(['--game', 'game.exe', '--output', 'out', '--no-steam']) == 0
        assert invoke.call_args.kwargs['steam'] is False
    with mock.object(patcher, 'restore_steam') as restore:
        assert patcher.main(['--restore-steam', '--output', 'out']) == 0 and restore.called


def main():
    system = patcher.host_platform()
    with tempfile.TemporaryDirectory(prefix="patcher test ü ") as tmp:
        root = Path(tmp)
        game = root / "steamapps" / "common" / "WEBFISHING"
        game.mkdir(parents=True)
        (game / "webfishing.exe").write_bytes(b"not the game")
        raises(patcher.check_game, game / "webfishing.exe", text="Unsupported WEBFISHING version")
        raises(patcher.check_game, root / "missing.exe", text="not found")
        exe = game / "webfishing.exe"
        raises(patcher.check_output, exe, game / "Refreshed", system, text="overlaps")
        raises(patcher.check_output, exe, root / "steamapps" / "common", system, text="overlaps")
        raises(patcher.check_output, exe, root / "steamapps" / "compatdata" / "3146520" / "x", system, text="overlaps")
        raises(patcher.check_output, exe, patcher.user_data_dir(system) / "webfishing-godot4" / "x", system, text="overlaps")
        existing = root / "Refreshed out"
        existing.mkdir()
        raises(patcher.check_output, exe, existing, system, text="already exists")
        assert patcher.check_output(exe, existing, system, force=True) == existing.absolute()
        assert patcher.check_output(exe, root / "new out", system) == (root / "new out").absolute()

        # pinned checksums
        archive = root / "tool.zip"
        with zipfile.ZipFile(archive, "w") as z: z.writestr("tool.bin", b"x")
        url = archive.as_uri()
        patcher.TOOLS[system]["test"] = (url, "0" * 64, "tool.bin")
        raises(patcher.fetch_tool, "test", system, root / "cache", lambda m: None, text="Checksum mismatch")
        assert not list((root / "cache").glob("*.part")) and not (root / "cache" / "tool.zip").exists()
        patcher.TOOLS[system]["test"] = (url, hashlib.sha256(archive.read_bytes()).hexdigest(), "tool.bin")
        assert patcher.fetch_tool("test", system, root / "cache", lambda m: None).read_bytes() == b"x"
        patcher.TOOLS[system]["test"] = ((root / "absent.zip").as_uri(), "0" * 64, "tool.bin")
        raises(patcher.fetch_tool, "test", system, root / "cache2", lambda m: None, text="Download failed")

        # failed install keeps the old output
        new = root / "staged"
        new.mkdir()
        (new / "game").write_text("new")
        (existing / "game").write_text("old")
        patcher.install(new, existing, force=True)
        assert (existing / "game").read_text() == "new" and not new.exists()
        assert not [p for p in root.iterdir() if p.name.startswith(".Refreshed out.old")]
        raises(lambda: patcher.install(root / "missing staged", existing, force=True), text="Cannot move")
        assert (existing / "game").read_text() == "new"
        raises(patcher.install, root / "missing staged", existing, text="already exists")
        replace = patcher.os.replace
        def interrupt_move(src, dst):
            if src == root / "interrupted staged": raise KeyboardInterrupt()
            return replace(src, dst)
        with mock.object(patcher.os, 'replace', side_effect=interrupt_move):
            try: patcher.install(root / "interrupted staged", existing, force=True)
            except KeyboardInterrupt: pass
            else: raise AssertionError("Expected interrupt")
        assert (existing / "game").read_text() == "new"
        assert not [p for p in root.iterdir() if p.name.startswith(".Refreshed out.old")]

        # Mozilla roots
        system_roots = len(patcher.ssl.create_default_context().get_ca_certs())
        assert patcher.CA_BUNDLE.is_file() and len(patcher.https_context().get_ca_certs()) >= max(100, system_roots)

        # keep_build
        job = patcher.Job(system, root / "kb-staging", root / "kb-cache", lambda m: None)
        (job.staging / "rec").mkdir(); (job.staging / "rec" / "project.godot").write_text("rec")
        (job.staging / "project" / ".godot").mkdir(parents=True); (job.staging / "project" / ".godot" / "imported").write_text("imp")
        patcher.save_kept_build(job)
        assert (job.cache / "build" / "key.json").is_file() and not (job.staging / "rec").exists()
        job.staging = root / "kb-staging2"; job.staging.mkdir()
        assert patcher.restore_kept_build(job) and (job.staging / "project" / ".godot" / "imported").read_text() == "imp"
        assert not (job.cache / "build").exists()
        patcher.save_kept_build(job)
        (job.cache / "build" / "key.json").write_text("[0]")
        job.staging = root / "kb-staging3"; job.staging.mkdir()
        assert not patcher.restore_kept_build(job) and not (job.cache / "build").exists() and not (job.staging / "rec").exists()

        # WinError 5/32 retries
        def busy(times):
            left = [times]
            def move(src, dst):
                if left[0]:
                    left[0] -= 1
                    error = PermissionError("in use")
                    error.winerror = 32
                    raise error
                return replace(src, dst)
            return move
        (new := root / "staged").mkdir()
        (new / "game").write_text("newer")
        notes = []
        with mock.object(patcher.os, 'replace', side_effect=busy(2)), mock.object(patcher.time, 'sleep'):
            patcher.install(new, existing, force=True, progress=notes.append)
        assert (existing / "game").read_text() == "newer" and len(notes) == 1 and "retrying" in notes[0]
        (new := root / "staged").mkdir()
        with mock.object(patcher.os, 'replace', side_effect=busy(10 ** 6)), mock.object(patcher, 'MOVE_RETRY_SECONDS', 0.05):
            raises(patcher.install, new, existing, True, text="Cannot replace")
        assert (existing / "game").read_text() == "newer"

        test_steam(root)

        for name in ('../outside.txt', '/absolute.txt', 'C:/outside.txt', 'helpers/../../outside.txt', 'helpers\\..\\outside.txt'):
            raises(patcher.checked_path, name)
        assert patcher.checked_path('helpers/resave.gd') == 'helpers/resave.gd'

        # stalled import gets killed and retried
        fake = root / "fake_godot.py"
        fake.write_text("import pathlib, sys, time\nflag = pathlib.Path(sys.argv[0]).with_suffix('.ran')\n"
                        "print('importing', flush=True)\n"
                        "if not flag.exists():\n    flag.write_text('1')\n    time.sleep(30)\n")
        patcher.WRAPPER, patcher.IMPORT_TIMEOUT, patcher.IMPORT_SILENCE = [sys.executable, str(fake)], 100, 2
        job = patcher.Job(system, root / "staging", root / "cache", lambda m: None)
        job.godot = "godot"
        started = time.monotonic()
        job.godot_run(root, "--import", log="import.log")
        assert time.monotonic() - started < 15, "A silent import must be retried without waiting for the full timeout"
        patcher.WRAPPER = []
        # slow but logging is fine
        chatty = [sys.executable, "-c", "import time\nfor i in range(8):\n    print(i, flush=True)\n    time.sleep(0.5)"]
        patcher.run(chatty, root / "chatty.log", None, silence=2)

    # conversion cache
    with tempfile.TemporaryDirectory(prefix="patcher cache ") as temp:
        job = patcher.Job(system, Path(temp) / "staging", Path(temp) / "cache", lambda m: None)
        (job.staging / "helpers").mkdir()
        (job.staging / "helpers/resave.gd").write_text("resave helper")
        files = {"conv/Scenes/a.tscn": b"scene", "conv/Scenes/a.tscn.import": b"import", "resave/x.tres": b"r",
                 "resave_dec/x.tres": b"d", "gen/Scenes/a.tscn.txt": b"g"}
        for name, data in {**files, "conv/Assets/big.png": b"media", "conv/.godot/cache.bin": b"cache"}.items():
            (job.staging / name).parent.mkdir(parents=True, exist_ok=True)
            (job.staging / name).write_bytes(data)
        assert not patcher.restore_intermediates(job)
        (job.cache / "intermediates-0000000000000000.zip").parent.mkdir(parents=True)
        (job.cache / "intermediates-0000000000000000.zip").write_bytes(b"old")
        patcher.save_intermediates(job)
        assert [p.name for p in job.cache.iterdir()] == [patcher._intermediates_cache(job).name], "old caches are removed"
        for tree in patcher.TEXT_TREES: shutil.rmtree(job.staging / tree)
        assert patcher.restore_intermediates(job)
        assert all((job.staging / name).read_bytes() == data for name, data in files.items())
        assert not (job.staging / "conv/Assets/big.png").exists() and not (job.staging / "conv/.godot").exists()
        patcher._intermediates_cache(job).write_bytes(b"not a zip")
        assert not patcher.restore_intermediates(job) and not patcher._intermediates_cache(job).exists()
        before = patcher._intermediates_cache(job)
        (job.staging / "helpers/resave.gd").write_text("changed helper")
        assert patcher._intermediates_cache(job) != before, "a changed resave helper needs a new cache"

        messages = []
        job.progress = messages.append
        with mock.object(patcher.tempfile, 'mkstemp', side_effect=PermissionError('read-only cache')):
            patcher.save_intermediates(job)
        assert 'read-only cache' in messages[-1] and not patcher._intermediates_cache(job).exists()
        with mock.object(patcher.os, 'replace', side_effect=OSError('cache write failed')):
            patcher.save_intermediates(job)
        assert 'cache write failed' in messages[-1] and not patcher._intermediates_cache(job).exists()
        assert not list(job.cache.glob('.intermediates-*')), 'failed cache writes must remove temporary files'

    # godot runtime
    with tempfile.TemporaryDirectory(prefix='refreshed runtime ') as temp:
        remote = io.BytesIO()
        with zipfile.ZipFile(remote, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('templates/web_release.zip', os.urandom(1 << 16))
            archive.writestr('templates/linux_release.x86_64', b'runtime ' * 4096)
        remote = remote.getvalue()
        fetched = []
        def serve(url, start, end):
            assert url == patcher.TEMPLATES
            fetched.append(end + 1 - start)
            return remote[start:end + 1], len(remote)
        digest = hashlib.sha256(b'runtime ' * 4096).hexdigest()
        with mock.object(patcher, 'fetch_range', side_effect=serve), mock.dict(patcher.RUNTIME, {'linux': ('templates/linux_release.x86_64', digest)}):
            path = patcher.fetch_runtime('linux', Path(temp), lambda _: None)
            assert path.read_bytes() == b'runtime ' * 4096 and sum(fetched) < len(remote) - (1 << 15), 'only the runtime entry is downloaded'
            fetched.clear()
            assert patcher.fetch_runtime('linux', Path(temp), lambda _: None) == path and not fetched, 'cached'
            path.write_bytes(b'damaged')
            assert patcher.fetch_runtime('linux', Path(temp), lambda _: None).read_bytes() == b'runtime ' * 4096
        with mock.object(patcher, 'fetch_range', side_effect=serve), mock.dict(patcher.RUNTIME, {'windows': ('templates/linux_release.x86_64', '0' * 64)}):
            raises(patcher.fetch_runtime, 'windows', Path(temp), lambda _: None, text='Checksum mismatch')
            assert not list(Path(temp).glob('*/*.part'))

    # menu logo
    with tempfile.TemporaryDirectory(prefix="patcher logo ") as temp:
        ui = Path(temp) / "Assets/Textures/UI"
        ui.mkdir(parents=True)
        logo, badges = bytearray(480 * 260 * 4), bytearray(480 * 260 * 4)
        at = lambda x, y: slice((y * 480 + x) * 4, (y * 480 + x) * 4 + 4)
        logo[at(45 + 10, 43 + 5)] = bytes((200, 0, 0, 255))
        badges[at(10, 5)] = bytes((0, 0, 200, 128))
        badges[at(0, 0)] = bytes((0, 255, 0, 255))
        patcher.write_png(ui / "logo2.png", 480, 260, logo)
        patcher.write_png(Path(temp) / "badges.png", 480, 260, badges)
        patcher.compose_logo(Path(temp), Path(temp) / "badges.png")
        _, _, out = patcher.read_png(ui / "refreshedlogoalpha.png")
        assert bytes(out[at(0, 0)]) == bytes((0, 255, 0, 255))
        blended = bytes(out[at(10, 5)])
        assert blended[3] == 255 and 95 < blended[0] < 105 and 95 < blended[2] < 105, blended

    # delta round trip, build_payload.py is private
    if Path(__file__).with_name("build_payload.py").is_file():
        import build_payload
        base = b"header\nkeys = PackedFloat32Array(0, 1, 2.5, 3.25, 4, 5)\n" + b"shared text that is long enough to be copied " * 4
        generated = b"PackedFloat32Array(0.0, 1.0, 2.50001, 3.25, 4.0, 5.0)"
        target = b"new header\nkeys = PackedFloat32Array(0, 1, 2.5, 3.25, 4, 5)\n" + b"shared text that is long enough to be copied " * 4 + b"tail"
        encoder = build_payload.Encoder(["a", "g"], [base.replace(b"2.5, 3.25", b"9, 9"), generated])
        ops, masked = encoder.encode(target, [0], [1])
        assert any(not isinstance(o, bytes) and o[0] == "hole" for o in ops) and masked
        data, masked_data, holes = patcher.apply_delta(build_payload.serialize(ops), [base.replace(b"2.5, 3.25", b"9, 9"), generated].__getitem__)
        assert all(holes) and patcher.sha256(masked_data) == masked and data.replace(b"0.0, 1.0, 2.50001, 3.25, 4.0, 5.0", b"0, 1, 2.5, 3.25, 4, 5") == target

    # godot 3 -> 4
    assert patcher.multimesh_buffers(b"\ntransform_array = PoolVector3Array( 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 )") == \
        [b"PackedFloat32Array(1, 2, 3, 10, 4, 5, 6, 11, 7, 8, 9, 12)"]
    scene = (b'bones/0/name = "arm"\nbones/0/parent = -1\nbones/0/rest = Transform3D( 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 2, 0 )\n'
             b'tracks/0/type = "transform"\ntracks/0/path = NodePath("Skeleton3D:arm")\ntracks/0/interp = 1\n'
             b'tracks/0/keys = PackedFloat32Array( 0, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1, 1 )')
    assert patcher.rebase_bone_tracks(scene, {}).split(b"\n")[0] == b"PackedFloat32Array(0.0, 1.0, 1.0, 2.0, 0.0)"
    assert patcher.renumber_ids(b'[sub_resource type="X" id="X_ab"]\na = SubResource("X_ab")') == b'[sub_resource type="X" id="0"]\na = SubResource("0")'
    gltf = b'{"images":[{"bufferView":0}],"bufferViews":[{"byteOffset":2,"byteLength":3}]}'
    glb = b"glTF" + struct.pack("<II", 2, 0) + struct.pack("<I", len(gltf)) + b"JSON" + gltf + struct.pack("<I", 8) + b"BIN\0" + b"xxabcxxx"
    assert patcher.glb_image(glb, 0) == b"abc"
    print("PASS test_patcher")


if __name__ == "__main__":
    main()
