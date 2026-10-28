"""Tests for build.py. Run from the repository root: python3 -m unittest discover -s tests -v"""
import contextlib
import copy
import datetime
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build  # noqa: E402


def parse(text):
    # split lines as build.py reads a file: on '\n' only, like Pi-hole
    return build.parse_lines(io.StringIO(text, newline='\n'))


class ParseTest(unittest.TestCase):
    """The rules of Pi-hole's own parser (FTL src/tools/gravity-parseList.c)."""

    def test_hosts_lines_give_exact_names(self):
        self.assertEqual(parse('0.0.0.0 ads.example.com\n127.0.0.1 b.example.net c.example.org\n'),
                         ({'ads.example.com', 'b.example.net', 'c.example.org'}, set()))

    def test_tabs_separate_names(self):
        self.assertEqual(parse('0.0.0.0\ttab.example.com\n'), ({'tab.example.com'}, set()))

    def test_plain_names_are_exact(self):
        self.assertEqual(parse('example.com\r\nsub.example.org  \n'), ({'example.com', 'sub.example.org'}, set()))

    def test_abp_lines_cover_subdomains(self):
        self.assertEqual(parse('||example.com^\n||zip^\n'), (set(), {'example.com', 'zip'}))

    def test_abp_lines_with_options_or_exceptions_are_dropped(self):
        self.assertEqual(parse('||example.com^$third-party\n@@||example.org^\n|example.net^\n||^\n'
                               '||no-caret.example\n'),
                         (set(), set()))

    def test_comment_and_header_lines_are_skipped(self):
        self.assertEqual(parse('# a.example.com\n! b.example.com\n; c.example.com\n[Adblock Plus 2.0]\n'),
                         (set(), set()))

    def test_inline_comments_are_removed(self):
        self.assertEqual(parse('0.0.0.0 a.example.com # tracker.example.com\nb.example.com#c.example.com\n'),
                         ({'a.example.com', 'b.example.com'}, set()))

    def test_cosmetic_filters_are_skipped(self):
        self.assertEqual(parse('example.com##.ad\nexample.org#@#.ad\nexample.net#?#div\n'), (set(), set()))

    def test_addresses_are_not_names(self):
        self.assertEqual(parse('0.0.0.0 0.0.0.0\n:: ::1\n192.0.2.1 2001:db8::1\n'), (set(), set()))

    def test_case_and_one_trailing_dot_are_normalised(self):
        self.assertEqual(parse('Ads.Example.COM.\n'), ({'ads.example.com'}, set()))

    def test_exact_names_need_a_dot(self):
        self.assertEqual(parse('localhost\nexample\nbroadcasthost\n'), (set(), set()))

    def test_invalid_names_are_dropped(self):
        long_label = 'a' * 64
        self.assertEqual(parse(f'ex!ample.com\na..example.com\n{long_label}.com\nexample.-com\nexample.com-\n'),
                         (set(), set()))

    def test_underscores_are_allowed(self):
        self.assertEqual(parse('_dmarc.example.com\n'), ({'_dmarc.example.com'}, set()))

    def test_a_line_starting_with_a_pipe_has_no_exact_names(self):
        self.assertEqual(parse('||example.com^ other.example.org\n'), (set(), {'example.com'}))

    def test_internationalised_names_are_kept_as_written(self):
        self.assertEqual(parse('bücher.example\n'), ({'bücher.example'}, set()))

    def test_names_are_written_as_the_source_has_them(self):
        # Pi-hole's own IDN conversion decides what a byte order mark means
        self.assertEqual(parse('\ufeffexample.com\n'), ({'\ufeffexample.com'}, set()))

    def test_a_lone_carriage_return_does_not_end_a_line(self):
        self.assertEqual(parse('a.example.com\rb.example.com\nc.example.com\r\n'), ({'c.example.com'}, set()))

    def test_only_ascii_whitespace_is_trimmed(self):
        self.assertEqual(parse('a.example.com\u00a0\nb.example.com\x0b\x0c\n'), ({'b.example.com'}, set()))

    def test_only_spaces_and_tabs_separate_names(self):
        self.assertEqual(parse('0.0.0.0\ta.example.com\u00a0b.example.com\n'), (set(), set()))


class PruneTest(unittest.TestCase):
    """Within a category, drop what a ||parent^ entry already blocks."""

    def test_exact_names_under_an_abp_entry_are_dropped(self):
        self.assertEqual(build.prune({'a.example.com', 'b.a.example.com', 'example.com', 'example.org'},
                                     {'example.com'}),
                         ({'example.org'}, {'example.com'}))

    def test_abp_entries_under_an_abp_parent_are_dropped(self):
        self.assertEqual(build.prune(set(), {'example.com', 'ads.example.com', 'zip', 'a.b.zip'}),
                         (set(), {'example.com', 'zip'}))

    def test_exact_names_cover_nothing(self):
        self.assertEqual(build.prune({'example.com', 'a.example.com'}, {'b.example.com'}),
                         ({'example.com', 'a.example.com'}, {'b.example.com'}))

    def test_look_alike_names_are_not_covered(self):
        self.assertEqual(build.prune({'notexample.com', 'example.com.au'}, {'example.com'}),
                         ({'notexample.com', 'example.com.au'}, {'example.com'}))


def read_back(directory, files):
    """What Pi-hole reads from the written files, per file."""
    result = {}
    for f in files:
        with open(os.path.join(directory, f['name']), encoding='utf-8') as fh:
            result[f['name']] = build.parse_lines(fh)
    return result


class WriteTest(unittest.TestCase):
    EXACT = {f'host{i}.example.com' for i in range(40)} | {'bücher.example'}
    ABP = {f'ads{i}.example.net' for i in range(40)} | {'zip'}

    def write(self, parts, exact=None, abp=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        files = build.write_category(tmp, 'porn', 'Adult content', parts,
                                     self.EXACT if exact is None else exact,
                                     self.ABP if abp is None else abp, '2026-10-24 05:30 UTC')
        return tmp, files

    def test_one_part_reads_back_unchanged(self):
        tmp, files = self.write(1)
        self.assertEqual([f['name'] for f in files], ['porn.txt'])
        self.assertEqual(read_back(tmp, files)['porn.txt'], (self.EXACT, self.ABP))

    def test_parts_hold_every_entry_once(self):
        tmp, files = self.write(4)
        self.assertEqual([f['name'] for f in files], ['porn-1.txt', 'porn-2.txt', 'porn-3.txt', 'porn-4.txt'])
        back = read_back(tmp, files).values()
        exact = [n for e, _ in back for n in e]
        abp = [n for _, a in back for n in a]
        self.assertEqual((sorted(exact), sorted(abp)), (sorted(self.EXACT), sorted(self.ABP)))
        self.assertTrue(all(e or a for e, a in back), 'a part is empty')

    def test_a_name_stays_in_its_part_whatever_else_is_listed(self):
        tmp1, files1 = self.write(4)
        tmp2, files2 = self.write(4, exact={'host7.example.com'}, abp=set())
        home1 = [n for n, (e, _) in read_back(tmp1, files1).items() if 'host7.example.com' in e]
        home2 = [n for n, (e, _) in read_back(tmp2, files2).items() if 'host7.example.com' in e]
        self.assertEqual(home1, home2)

    def test_parts_are_the_same_in_every_run(self):
        # Python randomises str hashes per process, so build in two processes and compare.
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import build; '
                  "build.write_category(sys.argv[2], 'porn', 'Adult content', 4, "
                  "{f'host{i}.example.com' for i in range(40)}, set(), 'now')")
        contents = []
        for seed in ('1', '2'):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp)
            subprocess.run([sys.executable, '-c', script, REPO, tmp], check=True,
                           env=dict(os.environ, PYTHONHASHSEED=seed))
            contents.append([open(os.path.join(tmp, f'porn-{i}.txt')).read() for i in range(1, 5)])
        self.assertEqual(contents[0], contents[1])

    def test_files_start_with_a_comment_header_and_are_sorted(self):
        tmp, files = self.write(2)
        with open(os.path.join(tmp, 'porn-2.txt'), encoding='utf-8') as fh:
            lines = fh.read().splitlines()
        header = [line for line in lines if line.startswith('#')]
        entries = [line for line in lines if not line.startswith('#')]
        self.assertEqual(lines[:len(header)], header, 'comments only at the top')
        self.assertIn('# Title: dns-list porn (part 2 of 2)', header)
        self.assertIn('# Updated: 2026-10-24 05:30 UTC', header)
        names = [line[2:-1] if line.startswith('||') else line for line in entries]
        self.assertEqual(names, sorted(names))

    def test_files_send_readers_to_each_sources_licence(self):
        tmp, files = self.write(1)
        with open(os.path.join(tmp, 'porn.txt'), encoding='utf-8') as fh:
            header = [line for line in fh.read().splitlines() if line.startswith('#')]
        self.assertIn('# Licences: each entry keeps the licence of its source, listed in '
                      'https://github.com/nycoagung/dns-list/blob/master/SOURCES.md', header)
        self.assertFalse([line for line in header if 'GPL' in line], 'merged entries are not ours to license')

    def test_reported_counts_and_sizes_match_the_files(self):
        tmp, files = self.write(3)
        self.assertEqual(sum(f['entries'] for f in files), len(self.EXACT) + len(self.ABP))
        for f in files:
            self.assertEqual(f['bytes'], os.path.getsize(os.path.join(tmp, f['name'])))
            e, a = read_back(tmp, [f])[f['name']]
            self.assertEqual(f['entries'], len(e) + len(a))


def source(name, url=None, domains=None):
    entry = {'name': name, 'homepage': 'https://example.org/' + name, 'licence': 'MIT'}
    entry.update({'url': url} if url else {'domains': domains})
    return entry


CONFIG = {'categories': {
    'ads': {'description': 'Ads', 'parts': 1, 'sources': [source('A', url='https://a.example/list'),
                                                          source('B', url='https://b.example/list')]},
    'porn': {'description': 'Adult', 'parts': 2, 'sources': [source('C', url='https://c.example/list')]},
    'custom': {'description': 'Hand-made', 'parts': 1, 'sources': [
        source('Mine', domains=['forum.example', 'video.example', 'chat.example'])]},
}}
PAGES = {
    'https://a.example/list': '0.0.0.0 ads.example.com\n0.0.0.0 track.example.com\n',
    'https://b.example/list': '||example.com^\nads.example.org\n',
    'https://c.example/list': ''.join(f'site{i}.example\n' for i in range(10)),
}
FIRST = datetime.datetime(2026, 10, 17, 5, 30, tzinfo=datetime.timezone.utc)
SECOND = datetime.datetime(2026, 10, 24, 5, 30, tzinfo=datetime.timezone.utc)
THIRD = datetime.datetime(2026, 10, 31, 5, 30, tzinfo=datetime.timezone.utc)
MIXED = {'categories': {'mal': {'description': 'Malware', 'parts': 1, 'sources': [
    source('Big', url='https://big.example/list'), source('Small', url='https://small.example/list')]}}}


def mixed_pages(small):
    """A 100-entry source and a smaller one, so the small one can collapse while the
    category as a whole stays within the 20% shrink limit."""
    return {'https://big.example/list': ''.join(f'big{i}.example\n' for i in range(100)),
            'https://small.example/list': ''.join(f'small{i}.example\n' for i in range(small))}


class ConfigTest(unittest.TestCase):

    def assertRefused(self, config, words):
        with self.assertRaises(ValueError) as caught:
            build.check_config(config)
        self.assertIn(words, str(caught.exception))

    def test_the_test_config_is_accepted(self):
        build.check_config(CONFIG)

    def test_each_source_needs_a_licence_and_a_homepage(self):
        for field in ('licence', 'homepage', 'name'):
            config = copy.deepcopy(CONFIG)
            del config['categories']['ads']['sources'][0][field]
            self.assertRefused(config, field)

    def test_a_source_has_a_url_or_domains_not_both(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['ads']['sources'][0]['domains'] = ['a.example.com']
        self.assertRefused(config, 'url or domains')
        config = copy.deepcopy(CONFIG)
        del config['categories']['ads']['sources'][0]['url']
        self.assertRefused(config, 'url or domains')

    def test_urls_must_be_https(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['ads']['sources'][0]['url'] = 'http://a.example/list'
        self.assertRefused(config, 'https')

    def test_hand_made_domains_must_be_names_pi_hole_accepts(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['custom']['sources'][0]['domains'] = ['forum.example', 'forum']
        self.assertRefused(config, '"forum"')
        for bad in (['a.example b.example'], [], ['ok.example', 7], 'forum.example'):
            config = copy.deepcopy(CONFIG)
            config['categories']['custom']['sources'][0]['domains'] = bad
            self.assertRefused(config, 'domains')

    def test_hand_made_domains_may_also_block_subdomains(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['custom']['sources'][0]['domains'] = ['forum.example', '||video.example^']
        build.check_config(config)

    def test_a_source_may_be_listed_only_once(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['porn']['sources'].append(source('again', url='https://a.example/list'))
        self.assertRefused(config, 'more than once')

    def test_category_names_and_parts_are_checked(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['Bad Name'] = config['categories'].pop('custom')
        self.assertRefused(config, 'Bad Name')
        config = copy.deepcopy(CONFIG)
        config['categories']['porn']['parts'] = 0
        self.assertRefused(config, 'parts')

    def test_file_names_may_not_collide(self):
        config = copy.deepcopy(CONFIG)
        config['categories']['porn-1'] = config['categories'].pop('custom')
        self.assertRefused(config, 'porn-1.txt')


class BuildTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def run_build(self, out, pages=PAGES, previous=None, config=CONFIG, now=FIRST, accept_shrink=()):
        def fetch(url, dest):
            body = pages[url]
            if isinstance(body, Exception):
                raise body
            with open(dest, 'w', encoding='utf-8', newline='') as fh:
                fh.write(body)
        return build.build(config, os.path.join(self.root, out),
                           previous_dir=previous and os.path.join(self.root, previous),
                           fetch=fetch, now=now, accept_shrink=accept_shrink)

    def read(self, out, name):
        with open(os.path.join(self.root, out, name), encoding='utf-8') as fh:
            return fh.read()

    def test_every_category_is_merged_and_written(self):
        stats = self.run_build('out')
        self.assertEqual({c: s['status'] for c, s in stats['categories'].items()},
                         {'ads': 'updated', 'porn': 'updated', 'custom': 'updated'})
        self.assertEqual(build.exit_code(stats), 0)
        ads = build.parse_lines(self.read('out', 'ads.txt').splitlines())
        self.assertEqual(ads, ({'ads.example.org'}, {'example.com'}))
        self.assertEqual(stats['categories']['ads']['entries'], 2)
        self.assertEqual([s['entries'] for s in stats['categories']['ads']['sources']], [2, 2])
        self.assertEqual(build.parse_lines(self.read('out', 'custom.txt').splitlines()),
                         ({'forum.example', 'video.example', 'chat.example'}, set()))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'out', 'porn-2.txt')))

    def test_downloaded_files_are_split_into_lines_as_pi_hole_does(self):
        pages = dict(PAGES, **{'https://c.example/list':
                               'a.example.com\rb.example.com\nc.example.com\u00a0\nd.example.com\r\n'})
        stats = self.run_build('out', pages=pages)
        self.assertEqual(stats['categories']['porn']['entries'], 1)
        parts = [build.parse_lines(self.read('out', f'porn-{i}.txt').splitlines()) for i in (1, 2)]
        self.assertEqual(set().union(*(e for e, _ in parts)), {'d.example.com'})

    def test_the_run_is_recorded_in_stats_and_readme(self):
        self.run_build('out')
        stats = json.loads(self.read('out', 'stats.json'))
        self.assertEqual(stats['built'], '2026-10-17 05:30 UTC')
        self.assertEqual(stats['categories']['porn']['files'][1]['name'], 'porn-2.txt')
        readme = self.read('out', 'README.md')
        for name in ('ads.txt', 'porn-1.txt', 'porn-2.txt', 'custom.txt'):
            self.assertIn(name, readme)

    def test_a_failed_source_keeps_the_last_good_category(self):
        self.run_build('prev')
        error = urllib.error.HTTPError('https://b.example/list', 404, 'Not Found', {}, io.BytesIO())
        self.addCleanup(error.close)
        pages = dict(PAGES, **{'https://b.example/list': error})
        stats = self.run_build('out', pages=pages, previous='prev', now=SECOND)
        ads = stats['categories']['ads']
        self.assertEqual(ads['status'], 'kept')
        self.assertEqual(ads['updated'], '2026-10-17 05:30 UTC')
        self.assertIn('B', ads['problems'][0])
        self.assertIn('404', [s for s in ads['sources'] if s['name'] == 'B'][0]['error'])
        self.assertEqual(self.read('out', 'ads.txt'), self.read('prev', 'ads.txt'))
        self.assertIn('ads: B: HTTP Error 404', self.read('out', 'README.md'))
        self.assertEqual(stats['categories']['porn']['status'], 'updated')
        self.assertEqual(build.exit_code(stats), 2)

    def test_a_failed_source_without_a_last_version_leaves_the_category_out(self):
        pages = dict(PAGES, **{'https://c.example/list': TimeoutError('timed out')})
        stats = self.run_build('out', pages=pages)
        self.assertEqual(stats['categories']['porn']['status'], 'missing')
        self.assertEqual(stats['categories']['porn']['files'], [])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'out', 'porn-1.txt')))
        self.assertEqual(build.exit_code(stats), 2)

    def test_a_source_with_nothing_usable_counts_as_failed(self):
        pages = dict(PAGES, **{'https://c.example/list': '<html>Service unavailable</html>\n'})
        stats = self.run_build('out', pages=pages)
        self.assertEqual(stats['categories']['porn']['status'], 'missing')
        self.assertIn('no usable entries', stats['categories']['porn']['problems'][0])

    def test_shrinking_by_more_than_a_fifth_keeps_the_last_version(self):
        self.run_build('prev')
        pages = dict(PAGES, **{'https://c.example/list': ''.join(f'site{i}.example\n' for i in range(7))})
        stats = self.run_build('out', pages=pages, previous='prev', now=SECOND)
        self.assertEqual(stats['categories']['porn']['status'], 'kept')
        self.assertIn('10 to 7', stats['categories']['porn']['problems'][0])
        self.assertIn('--accept-shrink porn', stats['categories']['porn']['problems'][0])

    def test_shrinking_by_a_fifth_is_still_published(self):
        self.run_build('prev')
        pages = dict(PAGES, **{'https://c.example/list': ''.join(f'site{i}.example\n' for i in range(8))})
        stats = self.run_build('out', pages=pages, previous='prev', now=SECOND)
        self.assertEqual(stats['categories']['porn']['status'], 'updated')
        self.assertEqual(stats['categories']['porn']['entries'], 8)

    def test_a_source_losing_more_than_half_counts_as_failed(self):
        self.run_build('prev', pages=mixed_pages(10), config=MIXED)
        stats = self.run_build('out', pages=mixed_pages(4), previous='prev', config=MIXED, now=SECOND)
        mal = stats['categories']['mal']
        self.assertEqual(mal['status'], 'kept')
        self.assertIn('Small', mal['problems'][0])
        self.assertIn('10 to 4', mal['problems'][0])
        self.assertIn('--accept-shrink mal', mal['problems'][0])

    def test_a_source_losing_half_is_still_used(self):
        self.run_build('prev', pages=mixed_pages(10), config=MIXED)
        stats = self.run_build('out', pages=mixed_pages(5), previous='prev', config=MIXED, now=SECOND)
        self.assertEqual((stats['categories']['mal']['status'], stats['categories']['mal']['entries']),
                         ('updated', 105))

    def test_a_failed_source_is_compared_with_its_last_good_count(self):
        self.run_build('prev', pages=mixed_pages(10), config=MIXED)
        self.run_build('mid', pages=mixed_pages(4), previous='prev', config=MIXED, now=SECOND)
        stats = self.run_build('out', pages=mixed_pages(4), previous='mid', config=MIXED, now=THIRD)
        self.assertEqual(stats['categories']['mal']['status'], 'kept')
        self.assertIn('10 to 4', stats['categories']['mal']['problems'][0])

    def test_an_accepted_shrink_is_published(self):
        self.run_build('prev')
        pages = dict(PAGES, **{'https://c.example/list': ''.join(f'site{i}.example\n' for i in range(7))})
        stats = self.run_build('out', pages=pages, previous='prev', now=SECOND, accept_shrink={'porn'})
        self.assertEqual((stats['categories']['porn']['status'], stats['categories']['porn']['entries']),
                         ('updated', 7))

    def test_an_accepted_source_drop_becomes_its_new_count(self):
        self.run_build('prev', pages=mixed_pages(10), config=MIXED)
        self.run_build('mid', pages=mixed_pages(4), previous='prev', config=MIXED, now=SECOND,
                       accept_shrink={'mal'})
        stats = self.run_build('out', pages=mixed_pages(4), previous='mid', config=MIXED, now=THIRD)
        self.assertEqual((stats['categories']['mal']['status'], stats['categories']['mal']['entries']),
                         ('updated', 104))

    def test_hand_made_lists_may_shrink_freely(self):
        self.run_build('prev')
        config = copy.deepcopy(CONFIG)
        config['categories']['custom']['sources'][0]['domains'] = ['forum.example']
        stats = self.run_build('out', previous='prev', config=config, now=SECOND)
        self.assertEqual(stats['categories']['custom']['status'], 'updated')
        self.assertEqual(stats['categories']['custom']['entries'], 1)

    def test_a_file_too_big_for_github_keeps_the_last_version(self):
        self.run_build('prev')
        with mock.patch.object(build, 'MAX_FILE_BYTES', 300):
            stats = self.run_build('out', previous='prev', now=SECOND)
        self.assertEqual(stats['categories']['porn']['status'], 'kept')
        self.assertIn('bytes', stats['categories']['porn']['problems'][0])
        self.assertEqual(self.read('out', 'porn-1.txt'), self.read('prev', 'porn-1.txt'))

    def test_a_kept_category_needs_its_last_files(self):
        self.run_build('prev')
        os.remove(os.path.join(self.root, 'prev', 'porn-2.txt'))
        pages = dict(PAGES, **{'https://c.example/list': TimeoutError('timed out')})
        stats = self.run_build('out', pages=pages, previous='prev', now=SECOND)
        self.assertEqual(stats['categories']['porn']['status'], 'missing')

    def test_categories_no_longer_configured_are_dropped(self):
        self.run_build('prev')
        config = copy.deepcopy(CONFIG)
        del config['categories']['custom']
        stats = self.run_build('out', previous='prev', config=config, now=SECOND)
        self.assertNotIn('custom', stats['categories'])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'out', 'custom.txt')))


class DownloadTest(unittest.TestCase):

    def setUp(self):
        self.dest = os.path.join(tempfile.mkdtemp(), 'list')
        self.addCleanup(shutil.rmtree, os.path.dirname(self.dest))

    def test_a_missing_list_is_not_retried(self):
        error = urllib.error.HTTPError('https://a.example/', 404, 'Not Found', {}, io.BytesIO())
        with mock.patch('urllib.request.urlopen', side_effect=error) as opened, mock.patch('time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                build.download('https://a.example/', self.dest)
        self.assertEqual(opened.call_count, 1)

    def test_a_busy_server_is_tried_three_times(self):
        error = urllib.error.HTTPError('https://a.example/', 503, 'Busy', {}, io.BytesIO())
        with mock.patch('urllib.request.urlopen', side_effect=error) as opened, mock.patch('time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                build.download('https://a.example/', self.dest)
        self.assertEqual(opened.call_count, 3)


class SourcesMarkdownTest(unittest.TestCase):

    def test_every_category_and_source_is_credited(self):
        text = build.render_sources_md(CONFIG)
        for heading in ('## ads', '## porn', '## custom'):
            self.assertIn(heading, text)
        self.assertIn('| [A](https://example.org/A) ([list](https://a.example/list)) | MIT |', text)
        self.assertIn('| [Mine](https://example.org/Mine) (3 domains listed in sources.json) | MIT |', text)

    def test_the_merged_lists_are_not_relicensed(self):
        text = build.render_sources_md(CONFIG)
        self.assertIn('keep the licences of the sources they come from', text)
        self.assertNotIn('published under GPL-3.0', text)


class RepositoryTest(unittest.TestCase):
    """The files of this repository agree with each other."""

    def setUp(self):
        self.config = build.load_config(os.path.join(REPO, 'sources.json'))

    def test_sources_md_is_up_to_date(self):
        with open(os.path.join(REPO, 'SOURCES.md'), encoding='utf-8') as fh:
            self.assertEqual(fh.read(), build.render_sources_md(self.config),
                             'run: python3 build.py sources-md > SOURCES.md')

    def test_changing_the_sources_rebuilds_the_lists(self):
        with open(os.path.join(REPO, '.github', 'workflows', 'build.yml'), encoding='utf-8') as fh:
            workflow = fh.read()
        push = workflow.split('push:', 1)[1].split('schedule:', 1)[0] if 'push:' in workflow else ''
        patterns = re.findall(r"'([^']+)'", push.split('paths:', 1)[1].splitlines()[0]) if 'paths:' in push else []
        for path in ('build.py', 'sources.json'):
            self.assertTrue(any(fnmatch.fnmatch(path, p) for p in patterns),
                            f'a push changing {path} should start a build (paths: {patterns})')

    def test_readme_gives_the_address_of_every_built_file(self):
        with open(os.path.join(REPO, 'README.md'), encoding='utf-8') as fh:
            readme = fh.read()
        for category, meta in self.config['categories'].items():
            for name in build.file_names(category, meta['parts']):
                self.assertIn(f'https://raw.githubusercontent.com/nycoagung/dns-list/lists/{name}', readme)

    def test_readme_gives_no_address_that_is_not_built(self):
        with open(os.path.join(REPO, 'README.md'), encoding='utf-8') as fh:
            readme = fh.read()
        built = {name for category, meta in self.config['categories'].items()
                 for name in build.file_names(category, meta['parts'])}
        listed = set(re.findall(r'https://raw\.githubusercontent\.com/nycoagung/dns-list/lists/([\w.-]+)', readme))
        self.assertEqual(listed - built, set())


class MainTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.sources = os.path.join(self.root, 'sources.json')
        self.write_sources({'categories': {'custom': {'description': 'Hand-made', 'parts': 1,
                                                      'sources': [source('mine', domains=['forum.example'])]}}})

    def write_sources(self, config):
        with open(self.sources, 'w') as fh:
            json.dump(config, fh)

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build.main(['--sources', self.sources, *args])
        return code, out.getvalue(), err.getvalue()

    def test_build_writes_the_lists_and_reports_each_category(self):
        code, out, _ = self.main('build', '--out', os.path.join(self.root, 'out'))
        self.assertEqual(code, 0)
        self.assertIn('custom', out)
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'out', 'custom.txt')))

    def test_sources_md_prints_the_credits(self):
        code, out, _ = self.main('sources-md')
        self.assertEqual((code, out), (0, build.render_sources_md(build.load_config(self.sources))))

    def test_accept_shrink_names_must_be_categories(self):
        code, _, err = self.main('build', '--out', os.path.join(self.root, 'out'), '--accept-shrink', 'custom,nope')
        self.assertEqual(code, 1)
        self.assertIn('nope', err)

    def test_accept_shrink_reaches_the_build(self):
        self.write_sources({'categories': {'porn': {'description': 'Adult', 'parts': 1,
                                                   'sources': [source('C', url='https://c.example/list')]}}})
        sizes = iter([10, 7, 7])

        def fake_download(url, dest):
            with open(dest, 'w') as fh:
                fh.write(''.join(f'site{i}.example\n' for i in range(next(sizes))))
        prev, kept, ok = (os.path.join(self.root, name) for name in ('prev', 'kept', 'ok'))
        with mock.patch.object(build, 'download', fake_download):
            self.assertEqual(self.main('build', '--out', prev)[0], 0)
            self.assertEqual(self.main('build', '--out', kept, '--previous', prev)[0], 2)
            self.assertEqual(self.main('build', '--out', ok, '--previous', prev, '--accept-shrink', 'porn')[0], 0)

    def test_a_broken_sources_file_is_reported_without_a_traceback(self):
        self.write_sources({'categories': {}})
        code, _, err = self.main('sources-md')
        self.assertEqual(code, 1)
        self.assertIn('sources.json', err)


if __name__ == '__main__':
    unittest.main()
