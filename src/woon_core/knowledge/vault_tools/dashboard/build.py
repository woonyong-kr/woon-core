#!/usr/bin/env python3
# Bake summary.json + data.json into dashboard.template.html -> dashboard.html
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))

def load(name):
    with open(os.path.join(HERE, name), encoding='utf-8') as f:
        return json.load(f)

summary = load('summary.json')
full = load('data.json')
data = {'summary': summary, 'notes': full.get('notes', [])}

js = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')

with open(os.path.join(HERE, 'dashboard.template.html'), encoding='utf-8') as f:
    tpl = f.read()

repl = '/*__VAULT_DATA_START__*/ ' + js + ' /*__VAULT_DATA_END__*/'
out = re.sub(r'/\*__VAULT_DATA_START__\*/.*?/\*__VAULT_DATA_END__\*/',
             lambda m: repl, tpl, count=1, flags=re.S)

if out == tpl and '__VAULT_DATA_START__' not in tpl:
    raise SystemExit('marker not found in template')

with open(os.path.join(HERE, 'dashboard.html'), 'w', encoding='utf-8') as f:
    f.write(out)

print('built dashboard.html: %d bytes, %d notes' % (len(out), len(data['notes'])))
