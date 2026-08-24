#!/usr/bin/env python3
# Obsidian vault analyzer -> JSON for live dashboard
import argparse
import os, json, re, time, subprocess
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.getcwd()

parser = argparse.ArgumentParser(description='Analyze the Woon knowledge repository.')
parser.add_argument(
    '--root',
    default=os.environ.get('WOON_KNOWLEDGE_ROOT', DEFAULT_ROOT),
    help='knowledge repository root (default: repository containing this script)',
)
parser.add_argument(
    '--output',
    default=HERE,
    help='generated JSON output directory (default: this script directory)',
)
args = parser.parse_args()
VAULT = os.path.abspath(os.path.expanduser(args.root))
OUTPUT = os.path.abspath(os.path.expanduser(args.output))
if not os.path.isdir(VAULT):
    parser.error('knowledge repository root does not exist: %s' % VAULT)
os.makedirs(OUTPUT, exist_ok=True)
SKIP_DIRS = {'.git', '.obsidian', '.drawio-backup',
             'assets', 'quartz', '.trash', 'node_modules', 'templates'}
now = time.time()

wikilink_re = re.compile(r'\[\[([^\]\|#]+)')
tag_re = re.compile(r'(?:^|\s)#([A-Za-z0-9_/\-가-힣]+)')

# --- git history: real last-modified (gm) and created (gc) per file ---
git_mtime, git_ctime = {}, {}
try:
    raw = subprocess.run(
        ['git', '-C', VAULT, 'log', '--no-merges',
         '--pretty=format:C%ct', '--name-only'],
        capture_output=True, text=True, timeout=60).stdout
    ts = None
    for line in raw.splitlines():
        if line.startswith('C') and line[1:].isdigit():
            ts = int(line[1:])
        elif line.strip() and ts is not None:
            p = line.strip()
            if p not in git_mtime:
                git_mtime[p] = ts   # first seen = newest
            git_ctime[p] = ts       # last seen = oldest
except Exception:
    pass

filelist = []
for root, dirs, files in os.walk(VAULT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
    for f in files:
        if f.lower().endswith('.md'):
            filelist.append(os.path.join(root, f))

notes = []
link_targets = Counter()
tag_counter = Counter()
folder_counter = Counter()
total_words = 0

for path in filelist:
    rel = os.path.relpath(path, VAULT)
    parts = rel.split(os.sep)
    top = parts[0] if len(parts) > 1 else '(root)'
    try:
        st = os.stat(path)
        with open(path, encoding='utf-8', errors='ignore') as fh:
            text = fh.read()
    except Exception:
        continue
    words = len(text.split())
    total_words += words
    tags = set()
    body = text
    fm = re.match(r'^---\n(.*?)\n---', text, re.S)
    if fm:
        fmtext = fm.group(1)
        for m in re.finditer(r'tags?:\s*\[([^\]]*)\]', fmtext):
            for t in m.group(1).split(','):
                t = t.strip().strip('"\'')
                if t: tags.add(t)
        for m in re.finditer(r'tags?:\s*\n((?:\s*-\s*.+\n?)+)', fmtext):
            for line in m.group(1).splitlines():
                t = line.strip().lstrip('-').strip().strip('"\'')
                if t: tags.add(t)
        body = text[fm.end():]
    for m in tag_re.finditer(body):
        tags.add(m.group(1))

    for t in tags:
        tag_counter[t] += 1
    links = set(m.group(1).strip() for m in wikilink_re.finditer(text))
    for l in links:
        link_targets[l.lower()] += 1
    name = os.path.splitext(os.path.basename(path))[0]
    folder_counter[top] += 1
    relkey = rel.replace(os.sep, '/')
    mtime = git_mtime.get(relkey, int(st.st_mtime))
    ctime = git_ctime.get(relkey, mtime)
    notes.append({'name': name, 'rel': rel, 'folder': top,
                  'mtime': mtime, 'ctime': ctime, 'size': st.st_size,
                  'words': words, 'tags': sorted(tags),
                  'nout': len(links)})

for n in notes:
    n['nback'] = link_targets.get(n['name'].lower(), 0)

DAY = 86400
orphans = [n for n in notes if n['nout'] == 0 and n['nback'] == 0]
empty = [n for n in notes if n['words'] < 5]
stale = [n for n in notes if now - n['mtime'] > 90 * DAY]
recent = sorted(notes, key=lambda n: n['mtime'], reverse=True)[:20]
stale_sorted = sorted(stale, key=lambda n: n['mtime'])[:30]

trend = Counter()      # notes modified per month
created = Counter()    # notes created per month
for n in notes:
    trend[datetime.fromtimestamp(n['mtime']).strftime('%Y-%m')] += 1
    created[datetime.fromtimestamp(n['ctime']).strftime('%Y-%m')] += 1

def slim(n):
    return {'name': n['name'], 'rel': n['rel'], 'folder': n['folder'],
            'mtime': n['mtime'], 'words': n['words'], 'nout': n['nout'],
            'nback': n['nback']}

out = {
    'generated': int(now),
    'vault': 'repo://knowledge',
    'totals': {
        'notes': len(notes), 'words': total_words,
        'tags': len(tag_counter), 'folders': len(folder_counter),
        'orphans': len(orphans), 'empty': len(empty), 'stale': len(stale),
    },
    'folders': folder_counter.most_common(),
    'tags': tag_counter.most_common(80),
    'recent': [slim(n) for n in recent],
    'stale': [slim(n) for n in stale_sorted],
    'orphans': [slim(n) for n in sorted(orphans, key=lambda n: n['mtime'])][:60],
    'empty': [slim(n) for n in empty][:60],
    'trend': sorted(trend.items()),
    'created': sorted(created.items()),
}
# full dataset (incl. every note) cached to disk for the query script
full = dict(out)
full['notes'] = [{'name': n['name'], 'rel': n['rel'], 'folder': n['folder'],
                  'tags': n['tags'], 'mtime': n['mtime'], 'words': n['words'],
                  'nout': n['nout'], 'nback': n['nback']} for n in notes]
def atomic(name, obj):
    p = os.path.join(OUTPUT, name)
    with open(p + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(p + '.tmp', p)
atomic('data.json', full)        # full dataset (server-side use)
atomic('summary.json', out)      # small summary for the dashboard

# shard the notes list into small files the artifact can read via read_file
import glob
for old in glob.glob(os.path.join(OUTPUT, 'notes-*.json')):
    try: os.remove(old)
    except OSError: pass
allnotes = full['notes']
SHARD = 130
nshards = (len(allnotes) + SHARD - 1) // SHARD
for i in range(nshards):
    atomic('notes-%d.json' % i, allnotes[i*SHARD:(i+1)*SHARD])
atomic('notes-index.json', {'shards': nshards, 'count': len(allnotes),
                            'generated': out['generated']})
print(json.dumps(out, ensure_ascii=False))
