#!/usr/bin/env python3
"""Merge public DNS blocklists into one list per category, for Pi-hole.

    python3 build.py build --out DIR [--previous DIR]   build every category into DIR
    python3 build.py sources-md                         print SOURCES.md from sources.json

Lists are read with the rules of Pi-hole's own parser (FTL src/tools/gravity-parseList.c),
so every entry written here is one Pi-hole accepts and blocks the same names.
"""
import argparse
import datetime
import http.client
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_URL = 'https://github.com/nycoagung/dns-list'
USER_AGENT = f'dns-list-builder (+{REPO_URL})'
MAX_FILE_BYTES = 90 * 1024 * 1024  # GitHub refuses files over 100 MiB
MAX_SHRINK = 0.20                  # a category losing more than this keeps its last version
MAX_SOURCE_DROP = 0.50             # a downloaded source losing more than this counts as failed
CATEGORY_NAME = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*$')
VALID_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-_.')
COMMON_ADDRESSES = {'0.0.0.0', '127.0.0.1', '::', '::1'}


def check_domain(name, fqdn_only):
    """Whether Pi-hole accepts a lower-case name: labels of 1-63 characters from [a-z0-9_-],
    at most 255 characters, a last label not starting or ending with '-', and at least
    one dot for an exact name. An internationalised name is checked in its punycode form
    but kept as written, so Pi-hole converts it exactly as it would from the source."""
    if not name.isascii():
        try:
            name = name.encode('idna').decode('ascii')
        except UnicodeError:
            return False
    if not 0 < len(name) <= 255 or not VALID_CHARS.issuperset(name):
        return False
    labels = name.split('.')
    if any(not 0 < len(label) <= 63 for label in labels):
        return False
    if fqdn_only and len(labels) < 2:
        return False
    return labels[-1][0] != '-' and labels[-1][-1] != '-'


def is_address(token):
    if token in COMMON_ADDRESSES:
        return True
    if token[0].isdigit():
        try:
            ipaddress.IPv4Address(token)
            return True
        except ValueError:
            pass
    if ':' in token[:5]:
        try:
            ipaddress.IPv6Address(token)
            return True
        except ValueError:
            pass
    return False


def parse_lines(lines):
    """(exact, abp) name sets from a list's lines, read as Pi-hole reads them. An exact
    name blocks only itself; an abp name comes from a "||name^" line and also blocks
    every subdomain."""
    exact, abp = set(), set()
    for raw in lines:
        line = raw.rstrip(' \t\n\r\x0b\x0c')  # what C's isspace() trims, as Pi-hole does
        if not line or line[0] in '!#;[':
            continue
        hash_at = line.find('#')
        if hash_at > 0 and line[hash_at + 1:hash_at + 2] in ('#', '$', '@', '?', '%'):
            continue  # cosmetic filters: ##, #$#, #@#, #?#, #%#
        if hash_at >= 0:
            line = line[:hash_at]
        for token in line.replace('\t', ' ').split(' '):
            if not token or is_address(token):
                continue
            if token.endswith('.'):
                token = token[:-1]
                if not token:
                    continue
            token = token.lower()
            if line[0] != '|' and check_domain(token, True):
                exact.add(token)
            elif token.startswith('||') and token.endswith('^') and len(token) > 3 \
                    and check_domain(token[2:-1], False):
                abp.add(token[2:-1])
    return exact, abp


def parents(name):
    """Every parent of a name: a.b.c -> b.c, c"""
    labels = name.split('.')
    return ('.'.join(labels[i:]) for i in range(1, len(labels)))


def prune(exact, abp):
    """Drop what Pi-hole blocks anyway: exact names equal to or under an abp entry,
    and abp entries under another abp entry."""
    abp = {name for name in abp if not any(p in abp for p in parents(name))}
    exact = {name for name in exact if name not in abp and not any(p in abp for p in parents(name))}
    return exact, abp


def file_names(category, parts):
    if parts == 1:
        return [category + '.txt']
    return [f'{category}-{i}.txt' for i in range(1, parts + 1)]


def write_category(out_dir, category, description, parts, exact, abp, updated):
    """Write a category as sorted files with a comment header. A big category is split
    into a fixed number of parts by a hash of each name, so a name always lands in the
    same part and Pi-hole's subscriptions stay valid. Returns one
    {'name', 'entries', 'bytes'} per file."""
    buckets = [[] for _ in range(parts)]
    for name in exact | abp:
        buckets[zlib.crc32(name.encode('utf-8')) % parts].append(name)
    total = sum(len(bucket) for bucket in buckets)
    files = []
    for index, (file_name, bucket) in enumerate(zip(file_names(category, parts), buckets), 1):
        bucket.sort()
        title = category if parts == 1 else f'{category} (part {index} of {parts})'
        count = f'{len(bucket):,}' if parts == 1 else \
            f'{len(bucket):,} of {total:,} in {parts} parts; subscribe to every part'
        header = [f'# Title: dns-list {title}',
                  f'# Description: {description}',
                  f'# Homepage: {REPO_URL}',
                  f'# Licences: each entry keeps the licence of its source, listed in '
                  f'{REPO_URL}/blob/master/SOURCES.md',
                  f'# Updated: {updated}',
                  f'# Entries: {count}']
        path = os.path.join(out_dir, file_name)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('\n'.join(header) + '\n')
            for name in bucket:
                fh.write(('||' + name + '^' if name in abp else name) + '\n')
        files.append({'name': file_name, 'entries': len(bucket), 'bytes': os.path.getsize(path)})
    return files


class SourceError(Exception):
    pass


def check_config(config):
    """Raise ValueError naming the first problem in a sources.json config."""
    categories = config.get('categories') if isinstance(config, dict) else None
    if not isinstance(categories, dict) or not categories:
        raise ValueError('sources.json needs a non-empty "categories" object')
    seen, files = set(), set()
    for category, meta in categories.items():
        if not CATEGORY_NAME.match(category):
            raise ValueError(f'category "{category}": use lower-case letters, digits and dashes')
        if not isinstance(meta.get('description'), str) or not meta['description'].strip():
            raise ValueError(f'{category}: needs a description')
        parts = meta.get('parts')
        if type(parts) is not int or parts < 1:
            raise ValueError(f'{category}: parts must be a whole number of at least 1')
        for name in file_names(category, parts):
            if name in files:
                raise ValueError(f'{category}: the file name {name} is already used')
            files.add(name)
        sources = meta.get('sources')
        if not isinstance(sources, list) or not sources:
            raise ValueError(f'{category}: needs at least one source')
        for src in sources:
            for field in ('name', 'homepage', 'licence'):
                if not isinstance(src.get(field), str) or not src[field].strip():
                    raise ValueError(f'{category}: a source has no {field}')
            label = f'{category}/{src["name"]}'
            if ('url' in src) == ('path' in src):
                raise ValueError(f'{label}: give a url or a path, not both')
            where = str(src.get('url') or src.get('path'))
            if 'url' in src and not where.startswith('https://'):
                raise ValueError(f'{label}: the url must start with https://')
            if 'path' in src and (os.path.isabs(where) or '..' in where.replace('\\', '/').split('/')):
                raise ValueError(f'{label}: the path must stay inside the repository')
            if where in seen:
                raise ValueError(f'{label}: {where} is listed more than once')
            seen.add(where)


def load_config(path):
    with open(path, encoding='utf-8') as fh:
        config = json.load(fh)
    check_config(config)
    return config


def download(url, dest, attempts=3):
    """Save url to dest. A client error (other than 429) fails at once; anything else
    is tried again after a pause, up to attempts times."""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response, open(dest, 'wb') as fh:
                shutil.copyfileobj(response, fh, 1 << 20)
            return
        except urllib.error.HTTPError as err:
            if err.fp is not None:
                err.close()
            if attempt == attempts or (400 <= err.code < 500 and err.code != 429):
                raise
        except (OSError, http.client.HTTPException):
            if attempt == attempts:
                raise
        time.sleep(30 * attempt)


def build_category(category, meta, out_dir, previous_dir, previous, fetch, repo_dir, tmp, updated, accept):
    """Merge one category's sources into out_dir, or fall back to its previous files.
    Each source's count is recorded so the next build can compare with it; a source
    that fails keeps its last good count. accept lets this build shrink the category."""
    exact, abp, sources, problems = set(), set(), [], []
    hint = f'if that is expected, build with --accept-shrink {category}'
    last_counts = {s.get('source'): s.get('entries') for s in (previous or {}).get('sources', [])}
    for src in meta['sources']:
        where = src.get('url') or src['path']
        last = last_counts.get(where)
        try:
            if 'path' in src:
                path = os.path.join(repo_dir, src['path'])
            else:
                path = os.path.join(tmp, 'download')
                fetch(src['url'], path)
            # lines end at '\n' only, as in Pi-hole: a lone '\r' stays inside its line
            with open(path, encoding='utf-8', errors='replace', newline='\n') as fh:
                found_exact, found_abp = parse_lines(fh)
            count = len(found_exact) + len(found_abp)
            if not count:
                raise SourceError('no usable entries')
            if 'url' in src and last and not accept and count < (1 - MAX_SOURCE_DROP) * last:
                raise SourceError(f'dropped from {last:,} to {count:,} entries; {hint}')
        except (OSError, http.client.HTTPException, SourceError) as err:
            sources.append({'name': src['name'], 'source': where, 'entries': last, 'error': str(err)})
            problems.append(f"{src['name']}: {err}")
            continue
        sources.append({'name': src['name'], 'source': where, 'entries': count, 'error': None})
        exact |= found_exact
        abp |= found_abp
    if not problems:
        exact, abp = prune(exact, abp)
        total = len(exact) + len(abp)
        hand_made = all('path' in src for src in meta['sources'])
        if previous and not hand_made and not accept and total < (1 - MAX_SHRINK) * previous.get('entries', 0):
            problems.append(f"shrank from {previous['entries']:,} to {total:,} entries; {hint}")
        else:
            files = write_category(out_dir, category, meta['description'], meta['parts'], exact, abp, updated)
            too_big = [f for f in files if f['bytes'] > MAX_FILE_BYTES]
            if not too_big:
                return {'status': 'updated', 'updated': updated, 'entries': total, 'files': files,
                        'sources': sources, 'problems': []}
            for f in files:
                os.remove(os.path.join(out_dir, f['name']))
            problems.append(f"{too_big[0]['name']} would have {too_big[0]['bytes']:,} bytes, over the "
                            f'{MAX_FILE_BYTES:,} limit: give the category more parts')
    last_files = (previous or {}).get('files') or []
    if previous_dir and last_files and all(os.path.isfile(os.path.join(previous_dir, f['name']))
                                           for f in last_files):
        for f in last_files:
            shutil.copyfile(os.path.join(previous_dir, f['name']), os.path.join(out_dir, f['name']))
        return {'status': 'kept', 'updated': previous.get('updated'), 'entries': previous.get('entries', 0),
                'files': last_files, 'sources': sources, 'problems': problems}
    return {'status': 'missing', 'updated': None, 'entries': 0, 'files': [], 'sources': sources,
            'problems': problems}


def build(config, out_dir, previous_dir=None, fetch=None, repo_dir=HERE, now=None, accept_shrink=()):
    """Build every category into out_dir and return the run's stats, also written to
    stats.json and README.md. A category with a failed source (one that cannot be read,
    has no usable entries, or lost more than MAX_SOURCE_DROP of its entries), a shrink
    of more than MAX_SHRINK, or a file over MAX_FILE_BYTES keeps its files from
    previous_dir (the last published build) instead. The categories named in
    accept_shrink may shrink in this build."""
    updated = (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%d %H:%M UTC')
    previous = {}
    if previous_dir:
        try:
            with open(os.path.join(previous_dir, 'stats.json'), encoding='utf-8') as fh:
                previous = json.load(fh).get('categories', {})
        except (OSError, ValueError):
            previous = {}
    os.makedirs(out_dir, exist_ok=True)
    stats = {'built': updated, 'categories': {}}
    with tempfile.TemporaryDirectory() as tmp:
        for category, meta in config['categories'].items():
            stats['categories'][category] = build_category(
                category, meta, out_dir, previous_dir, previous.get(category), fetch or download,
                repo_dir, tmp, updated, category in accept_shrink)
    with open(os.path.join(out_dir, 'stats.json'), 'w', encoding='utf-8') as fh:
        json.dump(stats, fh, indent=1, ensure_ascii=False)
        fh.write('\n')
    with open(os.path.join(out_dir, 'README.md'), 'w', encoding='utf-8') as fh:
        fh.write(render_readme(stats))
    return stats


def exit_code(stats):
    """0 when every category was rebuilt, 2 when any kept its last version or is missing."""
    return 0 if all(c['status'] == 'updated' for c in stats['categories'].values()) else 2


def render_readme(stats):
    lines = ['# dns-list: built lists', '',
             f"Built {stats['built']} by `build.py` on the master branch. What each list blocks, and how to "
             f'use it with Pi-hole: {REPO_URL}', '',
             '| File | Entries | Updated | Status |', '|---|---:|---|---|']
    problems = []
    for category, st in stats['categories'].items():
        for f in st['files']:
            lines.append(f"| [{f['name']}]({f['name']}) | {f['entries']:,} | {st['updated']} | {st['status']} |")
        if not st['files']:
            lines.append(f'| {category} | - | - | {st["status"]} |')
        problems += [f'- {category}: {p}' for p in st['problems']]
    if problems:
        lines += ['', '## Problems in this build', ''] + problems
    return '\n'.join(lines) + '\n'


def render_sources_md(config):
    lines = ['# Sources', '',
             'Each merged list is a compilation of the sources below, and its entries keep the licences of the '
             'sources they come from: some allow only non-commercial use, and some state no licence at all. '
             'The code and the hand-made lists in this repository are under the GPL-3.0 (see LICENSE). '
             'This file is made from `sources.json` by `python3 build.py sources-md`.', '']
    for category, meta in config['categories'].items():
        lines += [f'## {category}', '', meta['description'], '', '| Source | Licence |', '|---|---|']
        for src in meta['sources']:
            if 'url' in src:
                lines.append(f"| [{src['name']}]({src['homepage']}) ([list]({src['url']})) | {src['licence']} |")
            else:
                lines.append(f"| [{src['name']}]({src['path']}) | {src['licence']} |")
        lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Merge public DNS blocklists into one list per category.')
    parser.add_argument('--sources', default=os.path.join(HERE, 'sources.json'),
                        help='the sources file; hand-made lists are read next to it (default: %(default)s)')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('build', help='build every category')
    run.add_argument('--out', required=True, help='directory to write the lists to')
    run.add_argument('--previous', help='the last published lists, kept for a category that fails')
    run.add_argument('--accept-shrink', default='', metavar='CATEGORIES',
                     help='comma-separated categories allowed to shrink in this build')
    commands.add_parser('sources-md', help='print SOURCES.md')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.sources)
    except (OSError, ValueError) as err:
        print(f'{args.sources}: {err}', file=sys.stderr)
        return 1
    if args.command == 'sources-md':
        sys.stdout.write(render_sources_md(config))
        return 0
    accept = {name.strip() for name in args.accept_shrink.split(',') if name.strip()}
    unknown = sorted(accept - set(config['categories']))
    if unknown:
        print(f"--accept-shrink: no such category: {', '.join(unknown)}", file=sys.stderr)
        return 1
    stats = build(config, args.out, args.previous, repo_dir=os.path.dirname(os.path.abspath(args.sources)),
                  accept_shrink=accept)
    for category, st in stats['categories'].items():
        print(f"{category:18} {st['status']:8} {st['entries']:>11,}")
        for problem in st['problems']:
            print('    ' + problem)
    return exit_code(stats)


if __name__ == '__main__':
    sys.exit(main())
