#!/usr/bin/env python3
"""updater.py checks with fake releases"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import updater

TEST_KEY = bytes(range(32))
updater.UPDATE_PUBLIC_KEY = updater.public_key(TEST_KEY).hex()
updater.use_source(updater.REPOSITORY, updater.UPDATE_PUBLIC_KEY)


def fails(function, *args, text=""):
    try: function(*args)
    except updater.UpdateError as error:
        assert text in str(error), str(error)
        return
    raise AssertionError("Expected UpdateError")


def fixture(root, script=None):
    output = root / "Refreshed"
    output.mkdir()
    (output / "game.txt").write_text("old")
    original = root / "Original game/webfishing.exe"
    original.parent.mkdir()
    original.write_bytes(b"original stays untouched")
    state = root / ".Refreshed-updater"
    (state / "jobs/test").mkdir(parents=True)
    install = {"format": 1, "repository": updater.REPOSITORY, "version": "0.1.0-alpha.1", "system": "linux", "prereleases": True,
               "output": str(output), "game": str(original), "state": str(state), "python": str(Path(sys.executable).resolve())}
    install_path = output / "install.json"
    install_path.write_text(json.dumps(install))
    status = state / "jobs/test/status.json"
    if script is None:
        script = ('from pathlib import Path\nUPDATE_API = 1\ndef patch(game, output, keep_build=False, **kwargs):\n'
                  '    assert kwargs["steam"] is False and kwargs["force"] is True and keep_build is True\n'
                  '    assert (Path(output) / "game.txt").read_text() == "old"\n'
                  '    kwargs["progress"]("Rebuilding converted meshes from the recovered models")\n'
                  '    kwargs["before_install"]()\n'
                  '    (Path(output) / "game.txt").write_text("new")\n')
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({"target_commit": "a" * 40, "release_version": "0.1.0-alpha.2"}))
    package = io.BytesIO()
    with zipfile.ZipFile(package, 'w') as archive:
        for name, body in (("patcher.py", script), ("updater.py", "# fixture\n"), ("payload.zip", payload.getvalue()), ("NOTICE.txt", "notice")):
            archive.writestr(name, body)
    files = {updater.PACKAGE: package.getvalue()}
    files["update.json"] = updater.release_metadata(TEST_KEY, "0.1.0-alpha.2", "a" * 40, files[updater.PACKAGE]).encode()
    manifest = json.loads(json.loads(files["update.json"])["release"])
    release = {"id": 9, "tag_name": "v0.1.0-alpha.2", "body": "New boats", "published_at": "2026-10-18T00:00:00Z", "draft": False,
               "prerelease": True, "assets": [{"id": i, "name": name, "digest": "sha256:" + hashlib.sha256(body).hexdigest()} for i, (name, body) in enumerate(files.items(), 100)]}
    def request(url, maximum, destination=None, progress=None):
        assert url.startswith(updater.API + '/')
        if url.endswith('/releases?per_page=100'): data = json.dumps([release]).encode()
        elif url.endswith('/releases/9'): data = json.dumps(release).encode()
        else:
            selected = next(entry for entry in release['assets'] if url.endswith('/' + str(entry['id'])))
            data = files[selected['name']]
        assert len(data) <= maximum
        if destination:
            Path(destination).write_bytes(data)
            if progress: progress(len(data))
            return len(data), hashlib.sha256(data).hexdigest()
        return data
    return install, install_path, status, release, files, manifest, request


def replace_asset(release, files, name, body):
    files[name] = body
    next(entry for entry in release['assets'] if entry['name'] == name)['digest'] = 'sha256:' + hashlib.sha256(body).hexdigest()


def main():
    # caching has its own test at the end
    updater.LIST_SECONDS = updater.FRESH_SECONDS = -1
    assert updater.version_key('v1.0.0-alpha.2') < updater.version_key('1.0.0-alpha.10') < updater.version_key('1.0.0-beta.1') < updater.version_key('1.0.0')
    assert updater.version_key('1.0.0+abc') == updater.version_key('1.0.0+def')
    for version in ('1.0', '01.0.0', '1.0.0-alpha.01', 'vhello', None): fails(updater.version_key, version)
    incoming = urllib.request.Request(updater.API + '/releases/assets/1', headers={'Authorization': 'Bearer fixture-token'})
    redirected = updater.ReleaseRedirect().redirect_request(incoming, None, 302, 'redirect', {}, 'https://release-assets.githubusercontent.com/asset')
    assert redirected.get_header('Authorization') is None
    retained = updater.ReleaseRedirect().redirect_request(incoming, None, 302, 'redirect', {}, updater.API + '/releases/assets/2')
    assert retained.get_header('Authorization') == 'Bearer fixture-token'
    fails(updater.ReleaseRedirect().redirect_request, incoming, None, 302, 'redirect', {}, 'http://example.com/asset', text='insecure')
    fails(updater.request, 'https://example.com/arbitrary-python', 100, text='Unexpected')

    with tempfile.TemporaryDirectory(prefix='refreshed update ü ') as temp:
        root = Path(temp)
        install, path, status, release, files, manifest, request = fixture(root)
        saved_cwd = Path.cwd()
        (root / '.Refreshed-updater/update.lock').write_text('999999')  # killed updater
        try:
            with patch.object(updater, 'request', side_effect=request), patch.object(updater, 'process_running', return_value=False), \
                    patch.object(updater, 'atomic_json', wraps=updater.atomic_json) as written:
                assert updater.latest_release(install) == release
                assert updater.latest_release({**install, 'prereleases': False}) is None
                assert updater.latest_release({**install, 'version': '0.1.0'}) is None
                assert updater.release_manifest(release) == manifest
                assert updater.main(['--install', str(path), '--status', str(status), '--check']) == 0
                assert json.loads(status.read_text())['phase'] == 'available'
                assert (root / 'Refreshed/game.txt').read_text() == 'old'
                (root / '.Refreshed-updater/last-update.json').write_text('{}')  # earlier failure
                assert updater.main(['--install', str(path), '--status', str(status), '--release', '9', '--parent-pid', '123']) == 0
                assert json.loads(status.read_text())['phase'] == 'done'
                assert not (root / '.Refreshed-updater/last-update.json').exists()
                shares = [call.args[1]['progress'] for call in written.call_args_list if call.args[1].get('phase') == 'building']
                assert shares and shares == sorted(shares) and 0 <= shares[0] and shares[-1] < 1, shares
                assert any(call.args[1].get('message') == 'Rebuilding the game' for call in written.call_args_list)
                recorded = json.loads(path.read_text())
                assert recorded['notes'] == 'New boats' and len(recorded['installed_at']) == 10
                assert (root / 'Refreshed/game.txt').read_text() == 'new'
                assert (root / 'Original game/webfishing.exe').read_bytes() == b'original stays untouched'
                updater.release_lock(updater.acquire_lock(root / '.Refreshed-updater/update.lock'))
                assert not list((root / '.Refreshed-updater').glob('release-*'))
                ready = [call.args[1] for call in written.call_args_list if call.args[1].get('phase') == 'ready']
                assert ready and ready[0]['version'] == 'v0.1.0-alpha.2' and 'Quit the game' in ready[0]['message']
        finally: os.chdir(saved_cwd)

    for failure in ('checksum', 'build', 'metadata', 'signature', 'locked', 'timeout', 'moved', 'install'):
        with tempfile.TemporaryDirectory(prefix='refreshed failed update ') as temp:
            root = Path(temp)
            script = {'build': 'UPDATE_API = 1\nclass PatchError(Exception): pass\ndef patch(*args, **kwargs): raise RuntimeError("broken build")\n',
                      'moved': 'UPDATE_API = 1\nclass PatchError(Exception): pass\ndef patch(*args, **kwargs): raise PatchError("Game executable not found: x")\n',
                      'install': 'UPDATE_API = 1\nclass PatchError(Exception): pass\n'
                                 'def patch(*args, before_install, **kwargs):\n    before_install()\n    raise PatchError("Cannot replace x")\n'}.get(failure)
            install, path, status, release, files, manifest, request = fixture(root, script)
            if failure == 'metadata': next(entry for entry in release['assets'] if entry['name'] == 'update.json')['digest'] = 'sha256:' + '0' * 64
            held = updater.acquire_lock(root / '.Refreshed-updater/update.lock') if failure == 'locked' else None
            if failure == 'signature':
                document = json.loads(files['update.json'])
                document['signature'] = updater.sign(bytes(32), document['release'].encode()).hex()
                replace_asset(release, files, 'update.json', json.dumps(document).encode())
            def download(url, maximum, destination=None, progress=None):
                if failure == 'checksum' and destination and Path(destination).name == updater.PACKAGE:
                    tampered = files[updater.PACKAGE] + b'tampered'
                    Path(destination).write_bytes(tampered)
                    return len(tampered), hashlib.sha256(tampered).hexdigest()
                return request(url, maximum, destination, progress)
            saved_cwd = Path.cwd()
            try:
                # fails after the player quit
                with patch.object(updater, 'request', side_effect=download), patch.object(updater, 'process_running', return_value=failure != 'install'):
                    if failure == 'timeout':
                        with patch.object(updater, 'wait_for_game', side_effect=updater.UpdateError('game stayed open')):
                            assert updater.main(['--install', str(path), '--status', str(status), '--release', '9', '--parent-pid', '123']) == 1
                    elif failure == 'locked':  # another updater still finishing
                        for mode in (['--check'], ['--release', '9', '--parent-pid', '123']):
                            assert updater.main(['--install', str(path), '--status', str(status), *mode]) == 0
                            assert json.loads(status.read_text())['phase'] == 'busy'
                    else: assert updater.main(['--install', str(path), '--status', str(status), '--release', '9', '--parent-pid', '123']) == 1
                    assert json.loads(status.read_text())['phase'] == ('busy' if failure == 'locked' else 'error')
                    message = json.loads(status.read_text())['message']
                    if failure == 'signature': assert 'signature is not valid' in message, message
                    if failure == 'moved': assert 'updated or moved' in message, message
                    if failure == 'locked': assert 'finishing in the background' in message, message
                    if failure == 'metadata': assert 'checksum does not match' in message, message
                    notice = root / '.Refreshed-updater/last-update.json'
                    assert notice.is_file() == (failure == 'install'), failure
                    if failure == 'install': assert json.loads(notice.read_text()) == {'version': 'v0.1.0-alpha.2', 'message': 'Cannot replace x'}
                    assert (root / 'Refreshed/game.txt').read_text() == 'old'
                    assert (root / 'Original game/webfishing.exe').read_bytes() == b'original stays untouched'
                    assert not list((root / '.Refreshed-updater').glob('release-*'))
            finally:
                os.chdir(saved_cwd)
                if held: updater.release_lock(held)

    # version list and going back
    with tempfile.TemporaryDirectory(prefix='refreshed versions ') as temp:
        root = Path(temp)
        install, path, status, release, files, manifest, request = fixture(root)
        install['version'] = '0.1.0-alpha.3'
        path.write_text(json.dumps(install))
        saved_cwd = Path.cwd()
        try:
            # deleted files aren't offered
            empty = {**release, 'id': 10, 'tag_name': 'v0.1.0-alpha.1', 'assets': []}
            def listing(url, maximum, *rest, **named):
                if url.endswith('/releases?per_page=100'): return json.dumps([release, empty]).encode()
                return request(url, maximum, *rest, **named)
            with patch.object(updater, 'request', side_effect=listing), patch.object(updater, 'process_running', return_value=False):
                assert updater.main(['--install', str(path), '--status', str(status), '--list']) == 0
                listed = json.loads(status.read_text())
                assert listed['phase'] == 'versions' and listed['official'] and listed['releases'][0]['relation'] == 'older'
                assert [entry['id'] for entry in listed['releases']] == [9], listed['releases']
                assert listed['releases'][0]['date'] == '2026-10-18' and listed['releases'][0]['notes'] == 'New boats'
                assert updater.main(['--install', str(path), '--status', str(status), '--release', '9', '--parent-pid', '123']) == 1
                assert 'not newer' in json.loads(status.read_text())['message']
                assert (root / 'Refreshed/game.txt').read_text() == 'old'
                assert updater.main(['--install', str(path), '--status', str(status), '--release', '9', '--parent-pid', '123', '--allow-older']) == 0
                assert (root / 'Refreshed/game.txt').read_text() == 'new'
        finally: os.chdir(saved_cwd)

    # other sources
    for link in ('https://github.com/Someone/refreshed-fork', 'github.com/Someone/refreshed-fork.git', 'Someone/refreshed-fork', 'https://github.com/Someone/refreshed-fork/releases'):
        assert updater.parse_source(link) == 'Someone/refreshed-fork'
    for link in ('https://example.com/a/b', 'not a link', 'https://github.com/only-owner', '../../x/y'):
        fails(updater.parse_source, link, text='GitHub link')
    with tempfile.TemporaryDirectory(prefix='refreshed source ') as temp:
        root = Path(temp)
        install, path, status, release, files, manifest, request = fixture(root)
        fork_key = bytes(range(1, 33))
        fork_files = dict(files)
        fork_files['update.json'] = updater.release_metadata(fork_key, '0.1.0-alpha.2', 'a' * 40, files[updater.PACKAGE]).encode()
        fork = {**release, 'assets': [{'id': i, 'name': name, 'digest': 'sha256:' + hashlib.sha256(body).hexdigest()} for i, (name, body) in enumerate(fork_files.items(), 200)]}
        fork_api = 'https://api.github.com/repos/Someone/refreshed-fork'
        seen = []
        def fork_request(url, maximum, destination=None, progress=None):
            seen.append(url)
            if not url.startswith(fork_api + '/'): return request(url, maximum, destination, progress)
            if '/releases?' in url: return json.dumps([fork]).encode()
            selected = next(entry for entry in fork['assets'] if url.endswith('/' + str(entry['id'])))
            return fork_files[selected['name']]
        saved_cwd = Path.cwd()
        try:
            with patch.object(updater, 'request', side_effect=fork_request):
                assert updater.main(['--install', str(path), '--status', str(status), '--source', 'https://github.com/Someone/refreshed-fork']) == 0
                found = json.loads(status.read_text())
                assert found['phase'] == 'source' and not found['official'] and found['version'] == 'v0.1.0-alpha.2'
                assert found['fingerprint'] == updater.fingerprint(updater.public_key(fork_key).hex())
                assert not (root / '.Refreshed-updater/source.json').exists()
                assert updater.main(['--install', str(path), '--status', str(status), '--set-source', 'Someone/refreshed-fork', '--fingerprint', 'AAAA BBBB CCCC DDDD']) == 1
                assert 'key changed' in json.loads(status.read_text())['message']
                assert updater.main(['--install', str(path), '--status', str(status), '--set-source', 'Someone/refreshed-fork', '--fingerprint', found['fingerprint']]) == 0
                assert json.loads((root / '.Refreshed-updater/source.json').read_text())['repository'] == 'Someone/refreshed-fork'
                seen.clear()
                assert updater.main(['--install', str(path), '--status', str(status), '--check']) == 0
                assert json.loads(status.read_text())['phase'] == 'available' and all(url.startswith(fork_api) for url in seen)
                document = json.loads(fork_files['update.json'])  # other key
                document['signature'] = json.loads(files['update.json'])['signature']
                replace_asset(fork, fork_files, 'update.json', json.dumps(document).encode())
                assert updater.main(['--install', str(path), '--status', str(status), '--check']) == 1
                assert 'signature' in json.loads(status.read_text())['message']
                assert updater.main(['--install', str(path), '--status', str(status), '--set-source', 'official']) == 0
                assert not (root / '.Refreshed-updater/source.json').exists()
                assert updater.main(['--install', str(path), '--status', str(status), '--check']) == 0
                assert json.loads(status.read_text())['phase'] == 'available'
        finally:
            os.chdir(saved_cwd)
            updater.use_source(updater.REPOSITORY, updater.UPDATE_PUBLIC_KEY)

    # reset
    with tempfile.TemporaryDirectory(prefix='refreshed source reset ') as temp:
        install, path, status, release, files, manifest, request = fixture(Path(temp))
        source_path = Path(install['state']) / 'source.json'
        saved_cwd = Path.cwd()
        try:
            for damaged in ('{broken', 'null', '{"repository": "Someone/refreshed-fork"}'):
                source_path.write_text(damaged, encoding='utf-8')
                with patch.object(updater, 'request', side_effect=AssertionError('Reset must work offline')):
                    assert updater.main(['--install', str(path), '--status', str(status), '--set-source', 'official']) == 0
                assert not source_path.exists()
                assert json.loads(status.read_text()) == {'phase': 'source_saved', 'repository': updater.REPOSITORY, 'official': True}
                with patch.object(updater, 'request', side_effect=request):
                    assert updater.main(['--install', str(path), '--status', str(status), '--check']) == 0
                assert json.loads(status.read_text())['phase'] == 'available'
        finally: os.chdir(saved_cwd)

    # one lock holder
    with tempfile.TemporaryDirectory(prefix='refreshed lock ') as temp:
        lock = Path(temp) / 'update.lock'
        child = ('import sys, time\nsys.path.insert(0, sys.argv[1])\nimport updater\n'
                 'try: held = updater.acquire_lock(sys.argv[2])\nexcept updater.UpdateError: print("BUSY"); sys.exit()\n'
                 'print("GOT", flush=True)\ntime.sleep(float(sys.argv[3]))\nupdater.release_lock(held)\n')
        here = str(Path(__file__).resolve().parent)
        workers = [subprocess.Popen([sys.executable, '-c', child, here, str(lock), '4'], stdout=subprocess.PIPE, text=True) for _ in range(8)]
        results = [worker.communicate()[0].strip() for worker in workers]
        assert results.count('GOT') == 1 and results.count('BUSY') == 7, results
        holder = subprocess.Popen([sys.executable, '-c', child, here, str(lock), '60'], stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == 'GOT'
        fails(updater.acquire_lock, lock, text='already running')
        assert updater.update_running(Path(temp))
        holder.kill()
        holder.wait()
        assert not updater.update_running(Path(temp))

    # swap waits for quit
    running = iter([True, True, False])
    with tempfile.TemporaryDirectory() as temp, patch.object(updater, 'process_running', side_effect=lambda pid: next(running)), patch.object(updater.time, 'sleep'):
        updater.wait_for_game(123, Path(temp) / 'status.json', 'v1.0.0')
        assert json.loads((Path(temp) / 'status.json').read_text())['phase'] == 'ready'
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    assert not updater.process_running(child.pid)
    assert updater.process_running(os.getpid())
    # GUI installer backend
    with tempfile.TemporaryDirectory(prefix='refreshed setup ') as temp:
        root = Path(temp)
        script = ('from pathlib import Path\nimport json\nUPDATE_API = 1\nGAME_FILE = {"linux": "game.x86_64", "windows": "game.exe"}\n'
                  'def host_platform(): return "linux"\n'
                  'def patch(game, output, keep_build=False, **kwargs):\n'
                  '    assert kwargs["force"] is True and keep_build is True\n'
                  '    Path(output).mkdir()\n'
                  '    (Path(output).parent / ("." + Path(output).name + "-updater")).mkdir()\n'
                  '    if kwargs["steam"]: (Path(output) / "steam-launch.json").write_text(json.dumps({"installed_launch_options": "gamemoderun launch-refreshed.sh %command%"}))\n'
                  '    (Path(output) / "install.json").write_text("{}")\n')
        install, path, status, release, files, manifest, request = fixture(root, script)
        work = root / 'work'
        work.mkdir()
        setup_status = work / 'status.json'
        fresh = root / 'Fresh'
        with patch.object(updater, 'request', side_effect=request):
            assert updater.setup(['install', '--game', install['game'], '--output', str(fresh), '--status', str(setup_status), '--no-steam']) == 0
        assert updater.keep_build_setting(root / '.Fresh-updater')
        (root / '.Fresh-updater/settings.json').write_text('{"keep_build": false}')
        assert not updater.keep_build_setting(root / '.Fresh-updater') and updater.keep_build_setting(root / 'missing')
        done = json.loads(setup_status.read_text())
        assert done['phase'] == 'done' and done['executable'] == str(fresh / 'game.x86_64') and done['steam'] is False, done
        assert json.loads((fresh / 'install.json').read_text())['notes'] == 'New boats'
        # old release patcher
        manual = root / 'Manual setup ü'
        with patch.object(updater, 'request', side_effect=request):
            assert updater.setup(['install', '--game', install['game'], '--output', str(manual), '--status', str(setup_status), '--manual-steam']) == 0
        manual_done = json.loads(setup_status.read_text())
        assert manual_done['steam'] is False and str(manual / 'launch-refreshed.sh') in manual_done['launch_options']
        assert (manual / 'launch-refreshed.sh').is_file() and not (manual / 'steam-launch.json').exists()
        automatic = root / 'Automatic setup ü'
        with patch.object(updater, 'request', side_effect=request):
            assert updater.setup(['install', '--game', install['game'], '--output', str(automatic), '--status', str(setup_status)]) == 0
        automatic_done = json.loads(setup_status.read_text())
        assert automatic_done['steam'] is True
        assert automatic_done['launch_options'] == 'gamemoderun launch-refreshed.sh %command%', 'Show the actual installed parameters, including existing wrappers'
        unrelated = root / 'Documents'
        unrelated.mkdir()
        (unrelated / 'keep.txt').write_text('mine')
        with patch.object(updater, 'request', side_effect=request):
            assert updater.setup(['install', '--game', install['game'], '--output', str(unrelated), '--status', str(setup_status), '--no-steam']) == 1
        assert 'not a Refreshed installation' in json.loads(setup_status.read_text())['message']
        assert updater.setup(['uninstall', '--output', str(unrelated), '--status', str(setup_status)]) == 1
        assert (unrelated / 'keep.txt').read_text() == 'mine', 'Uninstall must never remove an unrelated folder'
        # uninstall removes kept build files only
        (root / 'cache/build/rec').mkdir(parents=True)
        (root / 'cache/tool').mkdir()
        Path(install['output'], 'install.json').write_text(json.dumps({**install, 'cache': str(root / 'cache')}))
        assert updater.setup(['uninstall', '--output', install['output'], '--status', str(setup_status)]) == 0
        assert not (root / 'cache/build').exists() and (root / 'cache/tool').exists()
        assert json.loads(setup_status.read_text())['phase'] == 'done'
        assert not Path(install['output']).exists() and not Path(install['state']).exists()
        assert Path(install['game']).read_bytes() == b'original stays untouched'
    # GitHub answer cache
    with tempfile.TemporaryDirectory(prefix='refreshed cache ') as temp:
        root = Path(temp)
        install, path, status, release, files, manifest, request = fixture(root)
        calls = []
        def counting(url, *args, **kwargs):
            calls.append(url)
            return request(url, *args, **kwargs)
        updater.LIST_SECONDS, updater.FRESH_SECONDS, updater.CACHE = 300, 60, root / 'cache'
        now = [1000.0]
        with patch.object(updater, 'request', side_effect=counting), patch.object(updater.time, 'time', side_effect=lambda: now[0]):
            for _ in range(3): updater.latest_release(install)
            assert sum('/releases?' in url for url in calls) == 1, calls
            updater.fresh = True
            now[0] += 30
            updater.published_releases(install)
            assert sum('/releases?' in url for url in calls) == 1, 'A manual check within a minute reuses the list'
            now[0] += 31
            updater.published_releases(install)
            assert sum('/releases?' in url for url in calls) == 2
            updater.fresh = False
            now[0] += 299
            updater.published_releases(install)
            assert sum('/releases?' in url for url in calls) == 2
            now[0] += 2
            updater.published_releases(install)
            assert sum('/releases?' in url for url in calls) == 3
            for _ in range(2): updater.release_manifest(release)
            assert sum('/releases/assets/' in url for url in calls) == 1, 'update.json is fetched once per asset'
        updater.LIST_SECONDS = updater.FRESH_SECONDS = -1
        updater.CACHE, updater.fresh = None, False
        # old job folders
        jobs = Path(install['state']) / 'jobs'
        old, recent = jobs / 'old', jobs / 'recent'
        old.mkdir(), recent.mkdir()
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        updater.prune_jobs(install['state'], status.parent)
        assert not old.exists() and recent.exists() and status.parent.exists()
    # rate limit
    limited = urllib.error.HTTPError(updater.API + '/releases', 403, 'rate limited', {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(int(time.time()) + 23 * 60 - 5)}, None)
    with patch.object(updater.urllib.request, 'build_opener', return_value=type('Opener', (), {'open': lambda self, *a, **k: (_ for _ in ()).throw(limited)})()):
        fails(updater.request, updater.API + '/releases?per_page=100', 100, text='Try again in 23 minutes')
    print('PASS test_updater release selection, authenticated redirect handling, checksums, update handoff, failure preservation, installer setup, safe uninstall, GitHub answer caching and rate limits')


if __name__ == '__main__':
    main()
