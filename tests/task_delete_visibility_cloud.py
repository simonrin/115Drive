"""Opt-in real 115 + CfAPI deletion/archive acceptance, generated fixtures only.

The default/--describe mode performs no login, local setup or cloud access.
Run --live-115 only after the candidate backend's unit tests are accepted.
The native test root, SQLite, transaction journals and cloud-history metadata
are isolated. Credentials are read in their original runtime location only.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import threading
import time
import uuid


CHECKS = [
    'local_delete_absent_from_unfiltered_original_cloud_directory',
    'archived_image_identity_sha1_and_download_preserved',
    'cloud_delete_propagates_to_native_local_root',
    'legacy_v1_archive_migrates_without_touching_recreated_original',
    'legacy_restarts_do_not_submit_duplicate_moves',
    'lost_move_response_recovers_without_duplicate_submission',
    'concurrent_original_replacement_is_preserved',
    'only_manifest_owned_fixture_roots_cleaned',
]


def describe():
    return dict(
        mode='describe_only', cloud_access=False, checks=CHECKS,
        fixture_cloud='/.twodrive-delete-visibility-<random UUID>/...',
        fixture_private='tests/runs/delete-visibility-<random UUID>/private',
        history_cloud='/115Drive 同步历史-<independently allocated random UUID>',
        credentials='Read the original runtime/private only; never copy or print credentials.',
        cleanup='Success: verify generated root IDs plus owner markers before deleting; '
                'failure: retain cloud fixtures/evidence, drain and unregister the generated native root.',
        scope='Actual 115 directory lists, file IDs, SHA1 and downloads; actual Windows CfAPI + SQLite. '
              'Not an official-client UI screenshot or physical-power-loss test.',
        command='python -X utf8 tests/task_delete_visibility_cloud.py --live-115 '
                '--runtime-root <existing-installation-directory>',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--describe', action='store_true')
    mode.add_argument('--live-115', action='store_true')
    parser.add_argument('--runtime-root', type=Path,
                        help='Existing installation; required for live tests, credentials are read only')
    parser.add_argument('--keep-cloud', action='store_true', help='Retain the verified generated fixtures on success')
    parser.add_argument('--stale-v1-only', action='store_true',
                        help='Only test a deliberately stale v1 baseline against a real retained cloud file')
    args = parser.parse_args()
    if not args.live_115:
        print(json.dumps(describe(), ensure_ascii=False, indent=2))
        return
    if args.runtime_root is None:
        parser.error('--runtime-root is required with --live-115')
    if os.name != 'nt':
        parser.error('--live-115 requires Windows with native CfAPI')

    workspace = Path(__file__).resolve().parents[1]
    runtime = args.runtime_root.resolve(strict=True)
    if not (runtime/'runtime/private/rclone.conf').is_file():
        parser.error('The original runtime rclone configuration does not exist')
    # Only this isolated interpreter gets a runtime anchor. Candidate modules
    # remain imported from the candidate source directory.
    os.environ['TWODRIVE_HOME'] = str(runtime)
    sys.path.insert(0, str(workspace))
    import twodrive as real_host
    from cloud_transactions import suffix
    from runtime_json import atomic_write_json
    from task_backend import OpenListBackend, RemoteChanged, version
    from task_engine import TaskEngine

    for method in ('migrate_delete_history', 'history_archive_info'):
        if not hasattr(OpenListBackend, method):
            raise RuntimeError('Candidate backend is not ready: ' + method)

    run_id = uuid.uuid4().hex
    run = workspace/'tests/runs'/('delete-visibility-' + run_id)
    run.mkdir(parents=True, exist_ok=False)
    (run/'script-source.py').write_bytes(Path(__file__).read_bytes())
    private = run/'private'
    private.mkdir()
    cloud = '/.twodrive-delete-visibility-' + run_id
    owner_name = 'fixture-owner-' + run_id + '.txt'
    owner = run/'owner.txt'
    owner.write_text('115Drive deletion acceptance fixture ' + run_id, encoding='utf-8')
    owner_sha = hashlib.sha1(owner.read_bytes()).hexdigest()
    began = time.monotonic()
    result = dict(ok=False, run_id=run_id, cloud_root=cloud, checks=[], phases=[],
                  scope=describe()['scope'], cloud_cleaned=False, native_unregistered=False)
    result['stale_v1_only'] = args.stale_v1_only
    result['source_sha256'] = {str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (Path(__file__).resolve(), workspace/'task_backend.py',
                                            workspace/'cloud_history.py', workspace/'task_engine.py')}
    manifest = dict(version=1, run_id=run_id, private=str(private), roots=[])
    readbacks = []
    engines = []
    submitted = Counter()
    cancelled = threading.Event()

    class SelectionComplete(Exception):
        """Finish the selected scenario, then run the shared final checks."""

    def save():
        result['seconds'] = round(time.monotonic() - began, 3)
        atomic_write_json(run/'result.json', result, indent=2, durable=True)
        atomic_write_json(run/'fixture-manifest.json', manifest, indent=2, durable=True)
        atomic_write_json(run/'raw-directory-readbacks.json', readbacks, indent=2, durable=True)

    def passed(name, **details):
        result['checks'].append(name)
        result['phases'].append(dict(name=name, **details))
        save()
        print('PASS ' + name + ' elapsed=' + str(result['seconds']), flush=True)

    class FixtureHost:
        PRIVATE = private
        RCLONE_CONF = real_host.PRIVATE/'rclone.conf'

        def __getattr__(self, name):
            return getattr(real_host, name)

        def transfer_flags(self):
            flags = real_host.transfer_flags()
            flags[flags.index('--cache-dir') + 1] = str(private/'rclone-cache')
            return flags

    host = FixtureHost()

    class FixtureBackend(OpenListBackend):
        """Use production transport while keeping all mutable metadata private."""
        def transfer(self, source, destination, event, *, kind='upload'):
            # The production transfer method reads PRIVATE/rclone.conf only;
            # a shallow delegate supplies the original read-only config path.
            # No transaction/history method runs against this delegate.
            delegate = copy.copy(self)
            class TransferHost:
                PRIVATE = host.RCLONE_CONF.parent
                def __getattr__(self, name):
                    return getattr(host, name)
            delegate.t = TransferHost()
            return OpenListBackend.transfer(delegate, source, destination, event, kind=kind)

        def post_identity(self, endpoint, fields):
            key = (endpoint, str(fields.get('fid[0]', fields.get('fid', ''))), str(fields.get('pid', '')))
            submitted[key] += 1
            super().post_identity(endpoint, fields)
            hook = getattr(self, 'after_identity', None)
            if hook:
                hook(endpoint, fields)

    def backend(path, task_id=None):
        task = dict(id=task_id or uuid.uuid4().hex, cloud_path=path)
        item = FixtureBackend(task, host)
        item.bind(private/'tasks'/task['id'])
        return item

    def mkdir(path):
        if path != cloud and not path.startswith(cloud + '/'):
            raise AssertionError('Fixture directory creation escaped generated cloud root')
        host.api('/api/fs/mkdir', dict(path='/115' + path), host.login())

    def raw(item, cid=None):
        if item.identity is None:
            item.connect()
        rows = host.native_115_list(item.cookie, str(cid or item.root_id), request=item._raw_request)
        names = [row['n'] for row in rows]
        assert len(names) == len(set(names)), 'Raw cloud directory has duplicate names'
        recorded = rows
        if str(cid) == '0':
            owned_ids = {entry['id'] for entry in manifest['roots']}
            recorded = [row for row in rows if str(row.get('cid')) in owned_ids and not row.get('fid')]
        readbacks.append(dict(cloud_path=item.task['cloud_path'] if str(cid) != '0' else '/ (owned roots only)',
                              cid=str(cid or item.root_id), elapsed=round(time.monotonic()-began, 3),
                              entries=[{key: row[key] for key in ('n', 'fid', 'cid', 'pid', 'sha', 's') if key in row}
                                       for row in recorded]))
        return {row['n']: row for row in rows}

    def row_identity(row):
        assert row.get('fid') and row.get('sha'), row
        return dict(kind='file', id=str(row['fid']), size=int(row['s']), sha1=row['sha'].lower())

    def expect_raw(item, name, expected):
        entry = raw(item).get(name)
        actual = row_identity(entry) if entry else None
        assert version(actual) == version(expected), (name, actual, expected)
        return actual

    def absence(item, name):
        rows = raw(item)
        assert name not in rows, rows
        assert not any(value.startswith('_twodrive-deleted-') or value.startswith('.twodrive-deleted-')
                       for value in rows), rows
        return sorted(rows)

    def make_source(label):
        # A valid tiny PNG, plus a unique trailing fixture nonce, exercises the
        # image-extension path without using any user's image.
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0ncAAAAASUVORK5CYII=')
        target = run/(label + '.png')
        target.write_bytes(png + ('\n' + run_id + ':' + label).encode('ascii'))
        return target

    def seed(item, name, label):
        return item.upload(name, make_source(label), None, cancelled)

    def new_scenario(name):
        path = cloud + '/' + name
        mkdir(path)
        item = backend(path)
        item.connect()
        return item

    def delete_operation(item, name):
        found = []
        for file in item.journal_dir.glob('*.json'):
            tx = json.loads(file.read_text(encoding='utf-8'))
            if tx.get('kind') == 'delete' and tx.get('path') == name:
                found.append(file.stem)
        assert len(found) == 1, found
        return found[0]

    def moves_for(fid):
        return sum(count for (endpoint, identity, _), count in submitted.items()
                   if endpoint == 'files/move' and identity == str(fid))

    def own_root(item, path):
        assert item.root_id and path.startswith('/') and path != '/'
        item.upload(owner_name, owner, None, cancelled)
        marker = row_identity(raw(item)[owner_name])
        assert marker['sha1'] == owner_sha
        record = dict(path=path, id=item.root_id, marker=owner_name, marker_identity=marker)
        manifest['roots'].append(record)
        save()
        return record

    def history_root(item):
        path = private/'cloud-history'/item.account_key/'root.json'
        assert path.resolve().is_relative_to(private.resolve())
        info = json.loads(path.read_text(encoding='utf-8'))
        root = info['root']
        root_path = root['path']
        assert root_path.startswith('/115Drive 同步历史-')
        token = root_path.rsplit('-', 1)[-1]
        assert len(token) == 32 and all(x in '0123456789abcdef' for x in token)
        existing = next((entry for entry in manifest['roots'] if entry['path'] == root_path), None)
        if existing:
            assert str(root['id']) == existing['id']
            return
        probe = backend(root_path)
        probe.connect()
        assert probe.root_id == str(root['id'])
        own_root(probe, root_path)

    def archive(item, operation, expected, label):
        info = item.history_archive_info(operation)
        assert info and info.get('stage') == 'moved' and version(info['expected']) == version(expected), info
        destination = PurePosixPath(info['destination_path'])
        assert destination.is_absolute() and len(destination.parts) >= 4, info
        history_root(item)
        root_path = destination.parts[1]
        assert any(record['path'] == '/' + root_path for record in manifest['roots'])
        probe = backend(str(destination.parent))
        probe.connect()
        rows = raw(probe)
        actual = row_identity(rows[destination.name])
        assert version(actual) == version(expected), (actual, expected)
        assert sum(str(row.get('fid')) == expected['id'] for row in rows.values()) == 1
        download = run/(label + '-archive-download.png')
        probe.download(destination.name, actual, download, cancelled)
        assert hashlib.sha1(download.read_bytes()).hexdigest() == expected['sha1']
        return dict(operation=operation, destination_path=str(destination), id=actual['id'], sha1=actual['sha1'])

    def drain_native(engine):
        assert engine.root.resolve().is_relative_to(run.resolve())
        if not engine.native.registered():
            engine.close()
            return
        # The fixture's always_keep files are normally already fully resident.
        # Retain all resident bodies, and remove only this fixture's incomplete
        # placeholders if a failed operation cannot provide their contents.
        paths = sorted(engine.root.rglob('*'), key=lambda path: len(path.parts), reverse=True)
        for path in paths:
            relative = path.relative_to(engine.root).as_posix()
            info = engine.native.state(path)
            if info is None:
                continue
            if info.get('partial'):
                if not engine.native.discard_online(relative):
                    engine.native.revert(relative)
            else:
                engine.native.revert(relative)
        engine.close()
        engine.native.unregister()
        assert not engine.native.registered()

    def clean_cloud():
        # Both generated cloud roots must have an exact ID and a fixture nonce
        # marker. Never clean a root derived from production cloud-history data.
        for record in reversed(manifest['roots']):
            path = record['path']
            assert path == cloud or path.startswith('/115Drive 同步历史-')
            assert record['marker'] == owner_name
            item = backend(path)
            item.connect()
            assert item.root_id == record['id'], 'Generated fixture root was replaced'
            expect_raw(item, owner_name, record['marker_identity'])
            item.post_identity('rb/delete', {'fid[0]': item.root_id})
            root_rows = raw(item, '0')
            assert not any(str(value.get('cid')) == record['id'] and not value.get('fid')
                           for value in root_rows.values()), 'Fixture deletion not confirmed'
            record['removed'] = True
            save()
        result['cloud_cleaned'] = True

    print(run, flush=True)
    save()
    try:
        mkdir(cloud)
        fixture = backend(cloud)
        fixture.connect()
        own_root(fixture, cloud)

        if args.stale_v1_only:
            # This deliberately constructs stale saved metadata. It does not
            # claim that 115 supports changing file bytes while retaining ID.
            from sync_state_store import load_state
            scenario = new_scenario('stale-v1-recovery')
            original = seed(scenario, 'retained.png', 'stale-v1-current')
            local = run/'local'
            local.mkdir()
            task = dict(id=scenario.task['id'], name='Stale v1 recovery fixture',
                        cloud_path=scenario.task['cloud_path'], local_path=str(local),
                        policy='always_keep', cache_hours=24, overrides={}, paused=False)
            directory = private/'tasks'/task['id']
            initial = TaskEngine(task, directory, scenario)
            engines.append(initial)
            initial.enable_state_store()
            initial.cycle()
            baseline = copy.deepcopy(initial.state['items'])
            assert version(baseline['retained.png']['remote']) == version(original)
            (local/'retained.png').unlink()
            initial.close()
            engines.remove(initial)
            # Ensure finally can drain the registered root if preparation fails.
            engines.append(initial)
            scenario = backend(task['cloud_path'], task['id'])
            scenario.connect()
            operation = uuid.uuid4().hex
            archive_name = '_twodrive-deleted-' + operation + '.png'
            scenario.rename_identity('retained.png', archive_name, original)
            stale_bytes = bytearray((run/'stale-v1-current.png').read_bytes())
            stale_bytes[-1] ^= 1
            (run/'constructed-stale-baseline.png').write_bytes(stale_bytes)
            stale = dict(original, sha1=hashlib.sha1(stale_bytes).hexdigest())
            assert stale['sha1'] != original['sha1'] and len(stale_bytes) == original['size']
            tx = dict(version=1, kind='delete', path='retained.png', expected=stale,
                      archive=archive_name, backend=scenario.identity)
            atomic_write_json(scenario.journal_dir/(operation + '.json'), tx, indent=2, durable=True)
            fresh = backend(task['cloud_path'], task['id'])
            engine = TaskEngine(task, directory, fresh)
            engines.remove(initial)
            engines.append(engine)
            blocked = []
            for label in ('first_cycle', 'next_cycle_during_cooldown'):
                try:
                    engine.cycle()
                except RemoteChanged as error:
                    blocked.append(dict(point=label, error=str(error)))
                else:
                    raise AssertionError('Stale v1 history unexpectedly acknowledged a deletion: ' + label)
                assert engine.state['items'] == baseline
                assert load_state(directory)['items'] == baseline
            receipt = fresh.history_archive_info(operation)
            assert receipt and receipt['stage'] == 'conflict'
            assert version(receipt['observed']) == version(original)
            cold = backend(task['cloud_path'], task['id'])
            try:
                cold.snapshot(transport=False)
            except RemoteChanged as error:
                blocked.append(dict(point='new_backend_snapshot', error=str(error)))
            else:
                raise AssertionError('A new backend ignored the persistent archive conflict')
            expect_raw(cold, archive_name, original)
            download = run/'stale-v1-retained-download.png'
            cold.download(archive_name, original, download, cancelled)
            assert hashlib.sha1(download.read_bytes()).hexdigest() == original['sha1']
            assert moves_for(original['id']) == 0
            assert engine.metrics['state_writes'] == 0
            assert engine.state['pending'] is None
            passed('constructed_stale_v1_baseline_blocks_restart_and_cooldown',
                   simulation='Real 115 file/listing and native SQLite baseline; deliberately stale saved v1 SHA1. '
                              'No claim of a real provider same-ID in-place content mutation.',
                   blocked=blocked, operation=operation, original_id=original['id'],
                   actual_sha1=original['sha1'], stale_sha1=stale['sha1'],
                   move_submissions=0, state_writes=0, baseline_items_preserved=len(baseline))
            drain_native(engine)
            result['native_unregistered'] = True
            if not args.keep_cloud:
                clean_cloud()
                passed(CHECKS[7], roots=len(manifest['roots']))
            else:
                result['cloud_retained_by_option'] = True
            result['ok'] = True
            raise SelectionComplete

        native = new_scenario('native-images')
        local = run/'local'
        local.mkdir()
        task = dict(id=native.task['id'], name='Deletion visibility fixture', cloud_path=native.task['cloud_path'],
                    local_path=str(local), policy='always_keep', cache_hours=24, overrides={}, paused=False)
        old = seed(native, 'local-delete.png', 'local-delete')
        remote_delete = seed(native, 'cloud-delete.png', 'cloud-delete')
        engine = TaskEngine(task, private/'tasks'/task['id'], native)
        engines.append(engine)
        engine.enable_state_store()
        engine.cycle()
        assert not engine.native.state(local/'local-delete.png')['partial']
        assert hashlib.sha1((local/'local-delete.png').read_bytes()).hexdigest() == old['sha1']
        (local/'local-delete.png').unlink()
        engine.cycle()
        assert 'local-delete.png' not in engine.state['items']
        raw_names = absence(native, 'local-delete.png')
        passed(CHECKS[0], raw_source_names=raw_names)
        operation = delete_operation(native, 'local-delete.png')
        archived = archive(native, operation, old, 'local-delete')
        passed(CHECKS[1], **archived)
        initial_moves = moves_for(old['id'])
        engine.cycle()
        assert moves_for(old['id']) == initial_moves

        expect_raw(native, 'cloud-delete.png', remote_delete)
        native.post_identity('rb/delete', {'fid[0]': remote_delete['id']})
        assert 'cloud-delete.png' not in raw(native)
        engine.cycle()
        assert not (local/'cloud-delete.png').exists()
        assert 'cloud-delete.png' not in engine.state['items']
        passed(CHECKS[2])

        legacy = new_scenario('legacy-v1')
        legacy_old = seed(legacy, 'restored-path.png', 'legacy-old')
        legacy_operation = uuid.uuid4().hex
        legacy_archive = '_twodrive-deleted-' + legacy_operation + suffix('restored-path.png')
        tx = dict(version=1, kind='delete', path='restored-path.png', expected=legacy_old,
                  archive=legacy_archive, backend=legacy.identity)
        atomic_write_json(legacy.journal_dir/(legacy_operation + '.json'), tx, indent=2, durable=True)
        legacy.rename_identity('restored-path.png', legacy_archive, legacy_old)
        replacement = seed(legacy, 'restored-path.png', 'legacy-new-other-client')
        assert replacement['id'] != legacy_old['id']
        legacy.migrate_delete_history([legacy_operation])
        expect_raw(legacy, 'restored-path.png', replacement)
        assert legacy_archive not in raw(legacy)
        archived = archive(legacy, legacy_operation, legacy_old, 'legacy-v1')
        passed(CHECKS[3], recreated_id=replacement['id'], **archived)
        before_moves = moves_for(legacy_old['id'])
        for _ in range(2):
            legacy = backend(legacy.task['cloud_path'], legacy.task['id'])
            legacy.connect()
            legacy.migrate_delete_history([legacy_operation])
            expect_raw(legacy, 'restored-path.png', replacement)
        assert moves_for(legacy_old['id']) == before_moves == 1
        passed(CHECKS[4], move_submissions=before_moves, restarts=2)

        uncertain = new_scenario('lost-move-response')
        uncertain_old = seed(uncertain, 'uncertain.png', 'uncertain-old')
        tripped = []
        def lose_response(endpoint, fields):
            if endpoint == 'files/move' and str(fields.get('fid[0]')) == uncertain_old['id'] and not tripped:
                tripped.append(True)
                raise TimeoutError('Injected: actual move completed but response was lost')
        uncertain.after_identity = lose_response
        caught = False
        try:
            uncertain.delete('uncertain.png', uncertain_old)
        except TimeoutError:
            caught = True
        assert tripped, 'The archive move fault was not exercised'
        uncertain_operation = delete_operation(uncertain, 'uncertain.png')
        for _ in range(2):
            uncertain = backend(uncertain.task['cloud_path'], uncertain.task['id'])
            uncertain.connect()
            uncertain.migrate_delete_history([uncertain_operation])
        absence(uncertain, 'uncertain.png')
        archived = archive(uncertain, uncertain_operation, uncertain_old, 'uncertain')
        assert moves_for(uncertain_old['id']) == 1
        passed(CHECKS[5], injected_response_loss=True, surfaced_timeout=caught, move_submissions=1, **archived)

        raced = new_scenario('concurrent-path-recreation')
        race_old = seed(raced, 'recreated.png', 'race-old')
        client = backend(raced.task['cloud_path'])
        client.connect()
        newer = []
        def recreate(step):
            if step == 'before_remote_delete' and not newer:
                client.rename_identity('recreated.png', 'other-client-original.png', race_old)
                newer.append(seed(client, 'recreated.png', 'race-new'))
        raced.fault = recreate
        rejected = False
        try:
            raced.delete('recreated.png', race_old)
        except RemoteChanged:
            rejected = True
        assert newer and rejected, 'Concurrent path replacement was not rejected'
        expect_raw(raced, 'recreated.png', newer[0])
        expect_raw(raced, 'other-client-original.png', race_old)
        race_operation = delete_operation(raced, 'recreated.png')
        for _ in range(2):
            raced = backend(raced.task['cloud_path'], raced.task['id'])
            raced.connect()
            raced.migrate_delete_history([race_operation])
            expect_raw(raced, 'recreated.png', newer[0])
            expect_raw(raced, 'other-client-original.png', race_old)
        assert moves_for(race_old['id']) == 0
        passed(CHECKS[6], original_id=race_old['id'], replacement_id=newer[0]['id'],
               archive_move_submissions=0, restarts=2)

        for item in engines:
            drain_native(item)
        result['native_unregistered'] = True
        if not args.keep_cloud:
            clean_cloud()
            passed(CHECKS[7], roots=len(manifest['roots']))
        else:
            result['cloud_retained_by_option'] = True
        result['ok'] = True
    except SelectionComplete:
        pass
    except BaseException as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        # Never delete cloud fixtures after a failed acceptance; preserve raw
        # state and journal evidence for diagnosis. Local native registration is
        # always unwound only within the exact newly generated test root.
        cleanup_errors = []
        for item in engines:
            try:
                drain_native(item)
            except Exception as error:
                cleanup_errors.append(type(error).__name__ + ': ' + str(error))
                try:
                    item.close()
                except Exception:
                    pass
        if cleanup_errors:
            result['cleanup_errors'] = cleanup_errors
            result['ok'] = False
        elif engines:
            result['native_unregistered'] = True
        result['identity_submissions'] = [dict(endpoint=key[0], id=key[1], parent_id=key[2], count=count)
                                          for key, count in sorted(submitted.items())]
        save()
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
