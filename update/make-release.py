#!/usr/bin/env python3
"""Build a release for thinksmart-update (run on the Mac): the client, and the system layer with it.

  python3 make-release.py CLIENT_DIR OUT_DIR --note "客户端改了什么" [--note ...]
        [--system IMAGE_REPO --system-note "系统层改了什么" ...] [--env-cache DIR] [--tag TAG]
      packs the client's tracked files, the Python environment for its requirements.lock (built in
      an arm64 Ubuntu 24.04 container, cached by lock hash), with --system the system layer of a
      thinksmart-ubuntu-image checkout (its VERSION, image/system-files.txt and the files it names)
      and latest.json (versions, notes, size and sha256 of each file) into OUT_DIR, laid out like
      GitHub Releases so OUT_DIR can be served as a test source as it is:
        OUT_DIR/latest/download/latest.json
        OUT_DIR/download/<tag>/thinksmart-client-X.Y.Z.tar.gz, client-env-<id>.tar.gz,
                               thinksmart-system-A.B.C.tar.gz
        OUT_DIR/release-<tag>.txt   title and description to paste into the GitHub release
      Publishing = a release with that tag on the CLIENT repository, marked Latest, with the files
      (done by a person). The system layer is published there too, never on the image repository:
      that one's Releases only hold images to flash. Tag: c<client>-s<system> (client-vX.Y.Z
      without --system).

The system layer's version is IMAGE_REPO/VERSION. Its packaged files must be committed, and a
version is tied to its files: the first release of A.B.C tags the image repository's HEAD
system-vA.B.C (a local tag); a later release with the same VERSION but other files stops and asks
for a new version, so two different packages never share a number. The client package also carries
the image repository's updater (deploy/updater/thinksmart-update): the client's
deploy/bootstrap-updater installs it on devices whose updater only knows the client (20261101 image).

No signature (decided 2026-11-01): devices fetch only over HTTPS and check every file's sha256.

The environment is built at its final device path, /opt/thinksmart/envs/<id>, with the same Python
(3.12, Ubuntu 24.04 arm64) as the device. pip in the container uses PIP_PROXY or PIP_INDEX_URL
from the environment when set.
"""
import argparse
import datetime
import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path


def _tar(out_path, members):
    """Deterministic tar.gz: root-owned, fixed mtime, sorted; members = [(arcname, path)]."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as t:
        for arc, path in sorted(members):
            info = t.gettarinfo(str(path), arc)
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            info.mtime = 0
            info.mode = 0o755 if info.isdir() or os.access(path, os.X_OK) else 0o644
            if info.isreg():
                with open(path, 'rb') as f:
                    t.addfile(info, f)
            else:
                t.addfile(info)
    with open(out_path, 'wb') as f, gzip.GzipFile(fileobj=f, mode='wb', mtime=0) as gz:
        gz.write(buf.getvalue())


def client_version(client):
    for line in (client / 'thinksmart_client' / '__init__.py').read_text().splitlines():
        if line.startswith('__version__'):
            return line.split('=', 1)[1].strip().strip('\'"')
    sys.exit('no __version__ in thinksmart_client/__init__.py')


def build_env(lock, env_id, cache):
    """client-env-<id>.tar.gz from the cache, or built in an arm64 ubuntu:24.04 container."""
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f'client-env-{env_id}.tar.gz'
    if out.exists():
        return out
    script = f'''set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
python3 -m venv /opt/thinksmart/envs/{env_id}
/opt/thinksmart/envs/{env_id}/bin/pip install -q --no-cache-dir -r /out/requirements-{env_id}.lock
/opt/thinksmart/envs/{env_id}/bin/pip uninstall -q -y pip
find /opt/thinksmart/envs/{env_id} -name __pycache__ -prune -exec rm -rf {{}} +
tar --owner=0 --group=0 --numeric-owner -czf /out/{out.name}.part -C /opt/thinksmart/envs/{env_id} .
'''
    # only the cache is mounted (Docker Desktop shares /Users but not every temp directory)
    (cache / f'requirements-{env_id}.lock').write_bytes(lock.read_bytes())
    cmd = ['docker', 'run', '--rm', '--platform', 'linux/arm64', '-v', f'{cache}:/out']
    # apt goes direct (ports.ubuntu.com is fine from here); pip takes PIP_PROXY / PIP_INDEX_URL if set
    for var in ('PIP_PROXY', 'PIP_INDEX_URL'):
        if os.environ.get(var):
            cmd += ['-e', f'{var}={os.environ[var]}']
    subprocess.run(cmd + ['ubuntu:24.04', 'bash', '-c', script], check=True)
    os.replace(cache / f'{out.name}.part', out)
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


SYSTEM_DIRS = ['kiosk', 'screen', 'xiaozhi', 'bluetooth', 'zram', 'update', 'VERSION',
               'image/system-files.txt', 'image/system-post.sh', 'image/thinksmart-ssh-hostkeys.service']


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=check)


def load_updater(repo):
    """The image repository's own updater, for its table reader and file-set id."""
    loader = importlib.machinery.SourceFileLoader('thinksmart_update', str(repo / 'update' / 'thinksmart-update'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def system_layer(repo):
    """(version, [(arcname, path)], requires, tree id) of a thinksmart-ubuntu-image checkout."""
    version = (repo / 'VERSION').read_text().strip()
    tu = load_updater(repo)
    if not tu.VERSION_RE.fullmatch(version):
        sys.exit(f'{repo}/VERSION: {version!r} is not x.y.z')
    if git(repo, 'status', '--porcelain', '--', *SYSTEM_DIRS).stdout.strip():
        sys.exit(f'{repo}: uncommitted changes in the system layer; commit them first')
    try:
        entries, requires = tu.read_table(repo)
    except tu.Fail as e:
        sys.exit(f'{repo}/{tu.TABLE}: {e}')
    files = [f for f in git(repo, 'ls-files', '-z', '--', *SYSTEM_DIRS).stdout.split('\0') if f]
    missing = {src for src, *_ in entries if src != '-'} - set(files)
    if missing:
        sys.exit(f'files in {tu.TABLE} not tracked by git: {" ".join(sorted(missing))}')
    tag = f'system-v{version}'
    if git(repo, 'rev-parse', '-q', '--verify', f'refs/tags/{tag}', check=False).returncode == 0:
        if git(repo, 'diff', '--quiet', tag, 'HEAD', '--', *SYSTEM_DIRS, check=False).returncode:
            sys.exit(f'the system layer changed since {tag} but VERSION is still {version}: '
                     f'raise VERSION (and commit) first')
    else:
        git(repo, 'tag', tag)
        print(f'tagged {repo.name} HEAD as {tag} (local)')
    return version, [(f, repo / f) for f in files], requires, tu.tree_id(repo, entries)


def build(args):
    client = Path(args.client).resolve()
    out = Path(args.out).resolve()
    version = client_version(client)
    system = system_layer(Path(args.system).resolve()) if args.system else None
    tag = args.tag or (f'c{version}-s{system[0]}' if system else f'client-v{version}')
    rel = out / 'download' / tag
    rel.mkdir(parents=True, exist_ok=True)

    files = subprocess.run(['git', '-C', str(client), 'ls-files', '-z'], capture_output=True,
                           check=True).stdout.decode().split('\0')
    members = [(f, client / f) for f in files if f and (client / f).is_file()]
    if system:
        # for devices whose updater only knows the client (deploy/bootstrap-updater)
        members.append(('deploy/updater/thinksmart-update', Path(args.system).resolve() / 'update' / 'thinksmart-update'))
    code = rel / f'thinksmart-client-{version}.tar.gz'
    _tar(code, members)

    lock = client / 'requirements.lock'
    env_id = hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
    cached = build_env(lock, env_id, Path(args.env_cache).expanduser().resolve())
    env = rel / cached.name
    if not env.exists():
        env.write_bytes(cached.read_bytes())

    manifest = {
        'component': 'client',
        'version': version,
        'published': datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec='seconds'),
        'notes': args.note,
        'files': {
            'code': {'path': f'{tag}/{code.name}', 'sha256': sha256(code), 'size': code.stat().st_size},
            'env': {'id': env_id, 'path': f'{tag}/{env.name}', 'sha256': sha256(env), 'size': env.stat().st_size},
        },
    }
    uploads = ['latest.json', code.name, env.name]
    if system:
        sversion, smembers, requires, tree = system
        spkg = rel / f'thinksmart-system-{sversion}.tar.gz'
        _tar(spkg, smembers)
        manifest['system'] = {'version': sversion, 'notes': args.system_note, 'requires': requires,
                              'tree': tree, 'file': {'path': f'{tag}/{spkg.name}', 'sha256': sha256(spkg),
                                                     'size': spkg.stat().st_size}}
        uploads.append(spkg.name)
    data = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
    latest = out / 'latest' / 'download'
    latest.mkdir(parents=True, exist_ok=True)
    (latest / 'latest.json').write_bytes(data)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))

    # what to paste into the release: people who land on this page should know it isn't for them
    title = f'在线更新：客户端 {version}' + (f' · 系统层 {system[0]}' if system else '')
    body = ['这些文件是设备在线更新时自动下载的，**不需要手动下载**：在设备的设置页「软件更新」里点「更新」就行。',
            '系统镜像项目：[thinksmart-ubuntu-image](https://github.com/yehuangbin948/thinksmart-ubuntu-image)。本发布仅供在线更新，不包含刷机镜像。',
            '', f'### 客户端 {version}', *[f'- {n}' for n in args.note or ['（没有说明）']]]
    if system:
        body += ['', f'### 系统层 {system[0]}', *[f'- {n}' for n in args.system_note or ['（没有说明）']]]
    text = f'标签：{tag}\n标题：{title}\n要上传的文件：{", ".join(uploads)}\n勾选 Set as the latest release\n\n说明：\n\n' + '\n'.join(body) + '\n'
    (out / f'release-{tag}.txt').write_text(text)
    print(f'\nrelease {tag} in {out}: upload {", ".join(uploads)}; title and description in release-{tag}.txt')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('client')
    p.add_argument('out')
    p.add_argument('--note', action='append', default=[])
    p.add_argument('--system', help='a thinksmart-ubuntu-image checkout: add its system layer')
    p.add_argument('--system-note', action='append', default=[])
    p.add_argument('--tag')
    p.add_argument('--env-cache', default='~/.cache/thinksmart-release/envs')
    build(p.parse_args())


if __name__ == '__main__':
    main()
