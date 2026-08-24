#!/usr/bin/env python3
# Explorer query over cached data.json. Returns a capped, sorted slice.
# usage: query.py <mode> <value> [limit]
#   mode: folder | tag | search | all
import sys, os, json

HERE = os.path.dirname(os.path.abspath(__file__))
mode = sys.argv[1] if len(sys.argv) > 1 else 'all'
value = sys.argv[2] if len(sys.argv) > 2 else ''
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 100

with open(os.path.join(HERE, 'data.json'), encoding='utf-8') as fh:
    notes = json.load(fh)['notes']

v = value.lower()
if mode == 'folder':
    sel = [n for n in notes if n['folder'] == value]
elif mode == 'tag':
    sel = [n for n in notes if v in [t.lower() for t in n['tags']]]
elif mode == 'search':
    sel = [n for n in notes if v in n['name'].lower() or v in n['rel'].lower()
           or any(v in t.lower() for t in n['tags'])]
else:
    sel = notes

sel.sort(key=lambda n: n['mtime'], reverse=True)
out = {'mode': mode, 'value': value, 'total': len(sel),
       'shown': min(limit, len(sel)), 'notes': sel[:limit]}
p = os.path.join(HERE, 'result.json')
with open(p + '.tmp', 'w', encoding='utf-8') as fh:
    json.dump(out, fh, ensure_ascii=False)
os.replace(p + '.tmp', p)
print(json.dumps(out, ensure_ascii=False))
