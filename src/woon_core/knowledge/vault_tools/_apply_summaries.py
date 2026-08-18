#!/usr/bin/env python3
"""TSV(path<TAB>summary)를 읽어 각 문서의 첫 H1 바로 아래에
'> 한 줄 요약: ...' 한 줄을 삽입한다. 이미 요약이 있으면 건너뛴다.
자동화 curation 루프의 '한 줄 요약 추가' 레인 실행 도구."""
import sys, os, re
ROOT = os.getcwd()
os.chdir(ROOT)
tsv = sys.argv[1]
done = 0
skip = 0
for line in open(tsv, encoding='utf-8'):
    line = line.rstrip('\n')
    if not line or '\t' not in line:
        continue
    path, summary = line.split('\t', 1)
    if not os.path.exists(path):
        print('NO FILE', path); continue
    t = open(path, encoding='utf-8').read()
    body = re.sub(r'(?s)^---.*?---', '', t)
    if re.search(r'^> ', body[:400], re.M) or '한 줄 요약' in t:
        skip += 1; continue
    lines = t.split('\n')
    out = []
    inserted = False
    for i, l in enumerate(lines):
        out.append(l)
        if not inserted and re.match(r'^# ', l):
            # H1 다음에 빈 줄 + 요약 + 빈 줄 삽입 (이미 다음이 빈 줄이면 중복 방지)
            nxt = lines[i+1] if i+1 < len(lines) else ''
            if nxt.strip() != '':
                out.append('')
            out.append(f'> 한 줄 요약: {summary.strip()}')
            out.append('')
            inserted = True
    if inserted:
        open(path, 'w', encoding='utf-8').write('\n'.join(out))
        done += 1
    else:
        print('NO H1', path)
print(f'삽입 {done} · 건너뜀 {skip}')
