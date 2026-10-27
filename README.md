# dns-list

DNS blocklists for [Pi-hole](https://pi-hole.net), one list per category. Each list merges
well-known public blocklists (see [SOURCES.md](SOURCES.md)) and, for a few categories, a
hand-made list from this repository, with duplicates removed. A GitHub Action rebuilds them
every Saturday, and within minutes of a change to the hand-made lists or the sources.

## The lists

| List | Blocks | Files |
|---|---|---|
| ads | Advertising, tracking and telemetry | `ads.txt` |
| malware | Malware, phishing, scams, fraud, spam and cryptojacking | `malware-1.txt` to `malware-4.txt` |
| porn | Adult content | `porn-1.txt` to `porn-4.txt` |
| gambling | Gambling | `gambling-1.txt`, `gambling-2.txt` |
| child-protection | Sites unsuitable for children: a mixed list (adult content, scams, drugs and more) | `child-protection.txt` |
| social-media | Social networks, chat and forums: Facebook, Instagram, TikTok, X (Twitter), Snapchat, WeChat, Discord, Reddit, Pinterest and more | `social-media.txt` |
| video | YouTube, Twitch and other video streaming, plus drama, anime and K-pop sites | `video.txt` |
| gaming | Online games, game stores and launchers, and DNS and VPN services that get around blocking | `gaming.txt` |
| dating | Dating services | `dating.txt` |
| drugs | Drugs | `drugs.txt` |
| hate-fakenews | Hate sites, junk sites and fake news | `hate-fakenews.txt` |

The three biggest lists are split into parts to stay under GitHub's file size limit.
**Subscribe to every part** of a split list: a name in one part can be covered by an
entry in another.

## Using them in Pi-hole

Under **Lists**, add the addresses you want (Pi-hole accepts several at once, separated by
spaces), assign each list to the groups it should filter, then update gravity.

```
https://raw.githubusercontent.com/nycoagung/dns-list/lists/ads.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/malware-1.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/malware-2.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/malware-3.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/malware-4.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/porn-1.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/porn-2.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/porn-3.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/porn-4.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/gambling-1.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/gambling-2.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/child-protection.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/social-media.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/video.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/gaming.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/dating.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/drugs.txt
https://raw.githubusercontent.com/nycoagung/dns-list/lists/hate-fakenews.txt
```

The [lists branch](https://github.com/nycoagung/dns-list/tree/lists) also has a README with
each file's entry count and the result of the last build. Its `stats.json` has the same,
plus file sizes and each source's count.

## How the lists are made

`build.py` (Python 3, no extra packages) downloads every source in `sources.json` and:

- reads each one with the rules of Pi-hole's own list parser, so it keeps exactly the
  entries Pi-hole would use: names from hosts files and plain lists, which block only
  themselves, and `||name^` entries, which also block every subdomain;
- merges each category's sources, dropping duplicates and any name already blocked by a
  `||name^` entry for one of its parents;
- splits a big category into a fixed number of parts by a hash of each name, so a name
  always stays in the same part and the addresses above never change;
- writes each file with a comment header that Pi-hole skips.

The GitHub Action runs the tests, builds, and replaces the `lists` branch with a single new
commit, so old builds don't pile up in the repository. You can also start it by hand from
the Actions tab.

**When something goes wrong**, a category keeps its last published files instead of being
replaced by a broken one. This happens when one of its sources cannot be downloaded, has no
usable entries, or has lost more than half its entries since the last build; when the
category as a whole shrinks by more than 20%; or when a file would pass GitHub's size limit.
Hand-made lists may shrink freely. The run is then marked as failed, GitHub sends an email,
and the README on the lists branch says what went wrong.

If a shrink is genuine (a source pruned dead domains, say), start the workflow by hand from
the Actions tab and put the category names in **accept_shrink**, separated by commas.
Locally, the same is `--accept-shrink malware,porn`.

## Hand-made lists

The `hand-made` folder has one file per category for domains the public sources miss:
`gaming.txt`, `social-media.txt` and `video.txt`. Add a domain on its own line (comments
start with `#`) and commit it to `master`: the change starts a build, and the domain is in
that category's list within minutes. For another category, create `hand-made/<category>.txt`
and add it to that category's sources in `sources.json`.

## Changing the sources

Edit `sources.json`. Each source needs a name, a `url` (or the `path` of a file in this
repository), a homepage and its licence; `parts` sets how many files a category is split
into. Renaming or removing a category, or changing its `parts`, changes the file names, so
update the Pi-hole subscriptions to match. Then:

```
python3 build.py sources-md > SOURCES.md
python3 -m unittest discover -s tests
python3 build.py build --out out
```

## Licence

The code and the hand-made lists are under the GPL-3.0 ([LICENSE](LICENSE)). The merged
lists are compilations of the sources in [SOURCES.md](SOURCES.md), and their entries keep
the licences of the sources they come from: some allow only non-commercial use, and some
state no licence at all.
