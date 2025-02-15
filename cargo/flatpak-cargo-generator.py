#!/usr/bin/env python3

__license__ = 'MIT'
import json
from urllib.parse import quote as urlquote
from urllib.parse import urlparse, ParseResult, parse_qs
import os
import glob
import subprocess
import argparse
import logging
import hashlib
import asyncio
import aiohttp
import toml

CRATES_IO = 'https://static.crates.io/crates'
CARGO_HOME = 'cargo'
CARGO_CRATES = f'{CARGO_HOME}/vendor'
VENDORED_SOURCES = 'vendored-sources'
COMMIT_LEN = 7

def canonical_url(url):
    'Converts a string to a Cargo Canonical URL, as per https://github.com/rust-lang/cargo/blob/35c55a93200c84a4de4627f1770f76a8ad268a39/src/cargo/util/canonical_url.rs#L19'
    logging.debug('canonicalising %s', url)
    # Hrm. The upstream cargo does not replace those URLs, but if we don't then it doesn't work too well :(
    url = url.replace('git+https://', 'https://')
    u = urlparse(url)
    # It seems cargo drops query and fragment
    u = ParseResult(u.scheme, u.netloc, u.path, None, None, None)
    u = u._replace(path = u.path.rstrip('/'))

    if u.netloc == 'github.com':
        u = u._replace(scheme = 'https')
        u = u._replace(path = u.path.lower())

    if u.path.endswith('.git'):
        u = u._replace(path = u.path[:-len('.git')])

    return u

def get_git_tarball(repo_url, commit):
    url = canonical_url(repo_url)
    path = url.path.split('/')[1:]

    assert len(path) == 2
    owner = path[0]
    if path[1].endswith('.git'):
        repo = path[1].replace('.git', '')
    else:
        repo = path[1]
    if url.hostname == 'github.com':
        return f'https://codeload.{url.hostname}/{owner}/{repo}/tar.gz/{commit}'
    elif url.hostname.split('.')[0] == 'gitlab':
        return f'https://{url.hostname}/{owner}/{repo}/repository/archive.tar.gz?ref={commit}'
    elif url.hostname == 'bitbucket.org':
        return f'https://{url.hostname}/{owner}/{repo}/get/{commit}.tar.gz'
    else:
        raise ValueError(f'Don\'t know how to get tarball for {repo_url}')

async def get_remote_sha256(url):
    logging.info(f"started sha256({url})")
    sha256 = hashlib.sha256()
    async with aiohttp.ClientSession() as http_session:
        async with http_session.get(url) as response:
            while True:
                data = await response.content.read(4096)
                if not data:
                    break
                sha256.update(data)
    logging.info(f"done sha256({url})")
    return sha256.hexdigest()

def load_toml(tomlfile='Cargo.lock'):
    with open(tomlfile, 'r') as f:
        toml_data = toml.load(f)
    return toml_data

def fetch_git_repo(git_url, commit):
    repo_dir = git_url.replace('://', '_').replace('/', '_')
    cache_dir = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
    clone_dir = os.path.join(cache_dir, 'flatpak-cargo', repo_dir)
    if not os.path.isdir(os.path.join(clone_dir, '.git')):
        subprocess.run(['git', 'clone', git_url, clone_dir], check=True)
    rev_parse_proc = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=clone_dir, check=True,
                                    stdout=subprocess.PIPE)
    head = rev_parse_proc.stdout.decode().strip()
    if head[:COMMIT_LEN] != commit[:COMMIT_LEN]:
        subprocess.run(['git', 'fetch', 'origin', commit], cwd=clone_dir, check=True)
        subprocess.run(['git', 'checkout', commit], cwd=clone_dir, check=True)
    return clone_dir

async def get_git_cargo_packages(git_url, commit):
    logging.info(f'Loading packages from git {git_url}')
    git_repo_dir = fetch_git_repo(git_url, commit)
    with open(os.path.join(git_repo_dir, 'Cargo.toml'), 'r') as r:
        root_toml = toml.loads(r.read())
    assert 'package' in root_toml or 'workspace' in root_toml
    packages = {}
    if 'package' in root_toml:
        packages[root_toml['package']['name']] = '.'
    if 'workspace' in root_toml:
        for member in root_toml['workspace']['members']:
            for subpkg_toml in glob.glob(os.path.join(git_repo_dir, member, 'Cargo.toml')):
                subpkg = os.path.relpath(os.path.dirname(subpkg_toml), git_repo_dir)
                with open(subpkg_toml, 'r') as s:
                    pkg_toml = toml.loads(s.read())
                packages[pkg_toml['package']['name']] = subpkg
    logging.debug(f'Packages in repo: {packages}')
    return packages

async def get_git_sources(package, tarball=False):
    name = package['name']
    source = package['source']
    commit = urlparse(source).fragment
    assert commit, 'The commit needs to be indicated in the fragement part'
    canonical = canonical_url(source)
    repo_url = canonical.geturl()

    cargo_vendored_entry = {
        repo_url: {
            'git': repo_url,
            'replace-with': VENDORED_SOURCES,
        }
    }
    rev = parse_qs(urlparse(source).query).get('rev')
    tag = parse_qs(urlparse(source).query).get('tag')
    branch = parse_qs(urlparse(source).query).get('branch')
    if rev:
        assert len(rev) == 1
        cargo_vendored_entry[repo_url]['rev'] = rev[0]
    elif tag:
        assert len(tag) == 1
        cargo_vendored_entry[repo_url]['tag'] = tag[0]
    elif branch:
        assert len(branch) == 1
        cargo_vendored_entry[repo_url]['branch'] = branch[0]

    if tarball:
        tarball_url = get_git_tarball(source, commit)
        git_sources = [{
            'type': 'archive',
            'archive-type': 'tar-gzip',
            'url': tarball_url,
            'sha256': await get_remote_sha256(tarball_url),
            'dest': f'{CARGO_CRATES}/{name}',
        }]
    else:
        git_sources = [{
            'type': 'git',
            'url': repo_url,
            'commit': commit,
            'dest': f'{CARGO_CRATES}/{name}',
        }]
    git_cargo_packages = await get_git_cargo_packages(repo_url, commit)
    pkg_subpath = git_cargo_packages[name]
    if pkg_subpath != '.':
        git_sources.append(
            {
                'type': 'shell',
                'commands': [
                    f'mv {CARGO_CRATES}/{name} {CARGO_CRATES}/{name}.repo',
                    f'mv {CARGO_CRATES}/{name}.repo/{pkg_subpath} {CARGO_CRATES}/{name}',
                    f'rm -rf {CARGO_CRATES}/{name}.repo'
                ]
            }
        )
    git_sources.append(
        {
            'type': 'file',
            'url': 'data:' + urlquote(json.dumps({'package': None, 'files': {}})),
            'dest': f'{CARGO_CRATES}/{name}', #-{version}',
            'dest-filename': '.cargo-checksum.json',
        }
    )

    return (git_sources, cargo_vendored_entry)

async def get_package_sources(package, cargo_lock, git_tarballs=False):
    metadata = cargo_lock.get('metadata')
    name = package['name']
    version = package['version']

    if 'source' not in package:
        logging.warning(f'{name} has no source')
        logging.debug(f'Package for {name}: {package}')
        return
    source = package['source']

    if source.startswith('git+'):
        return await get_git_sources(package, tarball=git_tarballs)

    key = f'checksum {name} {version} ({source})'
    if metadata is not None and key in metadata:
        checksum = metadata[key]
    elif 'checksum' in package:
        checksum = package['checksum']
    else:
        logging.warning(f'{name} doesn\'t have checksum')
        return
    crate_sources = [
        {
            'type': 'file',
            'url': f'{CRATES_IO}/{name}/{name}-{version}.crate',
            'sha256': checksum,
            'dest': CARGO_CRATES,
            'dest-filename': f'{name}-{version}.crate'
        },
        {
            'type': 'file',
            'url': 'data:' + urlquote(json.dumps({'package': checksum, 'files': {}})),
            'dest': f'{CARGO_CRATES}/{name}-{version}',
            'dest-filename': '.cargo-checksum.json',
        },
    ]
    return (crate_sources, {'crates-io': {'replace-with': VENDORED_SOURCES}})

async def generate_sources(cargo_lock, git_tarballs=False):
    sources = []
    cargo_vendored_sources = {
        VENDORED_SOURCES: {'directory': f'{CARGO_CRATES}'},
    }
    pkg_coros = [get_package_sources(p, cargo_lock, git_tarballs) for p in cargo_lock['package']]
    for pkg in await asyncio.gather(*pkg_coros):
        if pkg is None:
            continue
        else:
            pkg_sources, cargo_vendored_entry = pkg
        sources += pkg_sources
        cargo_vendored_sources.update(cargo_vendored_entry)

    sources.append({
        'type': 'shell',
        'dest': CARGO_CRATES,
        'commands': [
            'for c in *.crate; do tar -xf $c; done'
        ]
    })

    logging.debug(f'Vendored sources: {cargo_vendored_sources}')
    sources.append({
        'type': 'file',
        'url': 'data:' + urlquote(toml.dumps({
            'source': cargo_vendored_sources,
        })),
        'dest': CARGO_HOME,
        'dest-filename': 'config'
    })
    return sources

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cargo_lock', help='Path to the Cargo.lock file')
    parser.add_argument('-o', '--output', required=False, help='Where to write generated sources')
    parser.add_argument('-t', '--git-tarballs', action='store_true', help='Download git repos as tarballs')
    parser.add_argument('-d', '--debug', action='store_true')
    args = parser.parse_args()
    if args.output is not None:
        outfile = args.output
    else:
        outfile = 'generated-sources.json'
    if args.debug:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(level=loglevel)

    generated_sources = asyncio.run(generate_sources(load_toml(args.cargo_lock),
                                    git_tarballs=args.git_tarballs))
    with open(outfile, 'w') as out:
        json.dump(generated_sources, out, indent=4, sort_keys=False)

if __name__ == '__main__':
    main()
