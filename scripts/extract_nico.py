"""Extract cached Nico dictionary board HTML; never calls a judgment API."""
import argparse
import json
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from statistics import median


class BoardParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = {}
        self.current = None
        self.section = None
        self.time = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag in ('dt', 'dd') and ('st-bbs_reshead' in classes or 'st-bbs_resbody' in classes):
            number = int(attrs['data-res_no'])
            self.current = self.rows.setdefault(number, {'number': number, 'board_id': attrs.get('data-id_hash', ''), 'timestamp': '', 'text': ''})
            self.section = tag
        if self.section == 'dt' and 'bbs_resInfo_resTime' in classes:
            self.time = True
        if self.section == 'dd' and tag == 'br':
            self.current['text'] += '\n'
        if self.section == 'dd' and tag == 'img':
            self.current['text'] += attrs.get('alt', '')

    def handle_endtag(self, tag):
        if tag == 'span':
            self.time = False
        if tag == self.section:
            self.current = self.section = None
            self.time = False

    def handle_data(self, text):
        if self.current is not None:
            if self.section == 'dd':
                self.current['text'] += text
            elif self.time:
                self.current['timestamp'] += text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('data/niconico'))
    parser.add_argument('--limit', type=int, default=1000)
    args = parser.parse_args()
    rows = {}
    files = sorted((args.out/'raw').glob('[0-9]*.html'))
    for path in files:
        page = BoardParser()
        page.feed(path.read_text(encoding='utf-8'))
        if not page.rows:
            raise ValueError(f'No posts parsed: {path}')
        rows.update(page.rows)
    latest = max(rows)
    first = latest - args.limit + 1
    selected = [rows[n] for n in sorted(rows) if first <= n <= latest]
    for row in selected:
        row['text'] = row['text'].strip()
        row['timestamp'] = row['timestamp'].strip()
    deleted = [r for r in selected if r['text'] == '削除しました']
    usable = [r for r in selected if r['board_id'] and r['text'] and r['text'] != '削除しました']
    counts = Counter(r['board_id'] for r in usable)
    groups = defaultdict(list)
    aliases = {key: f'u{i:03d}' for i, key in enumerate(sorted(counts), 1)}
    for row in usable:
        groups[aliases[row['board_id']]].append(row['text'])
    def save(name, value):
        (args.out/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    def jsonl(name, values):
        (args.out/name).write_text(''.join(json.dumps(v, ensure_ascii=False)+'\n' for v in values))
    jsonl('posts.jsonl', selected)
    jsonl('community.jsonl', [{'user_id':key, 'posts':posts} for key, posts in groups.items()])
    save('users.json', [{'user_id':aliases[k], 'board_id':k, 'posts':n} for k,n in counts.most_common()])
    summary = {'pages':len(files), 'first_number':first, 'last_number':latest,
               'slots_requested':args.limit, 'parsed_posts':len(selected), 'usable_posts':len(usable),
               'missing_numbers':[n for n in range(first,latest+1) if n not in rows],
               'unusable_numbers':[r['number'] for r in selected if r not in usable],
               'ids_including_deleted':len({r['board_id'] for r in selected if r['board_id']}),
               'deleted_posts':len(deleted), 'unique_ids':len(counts), 'first_timestamp':selected[0]['timestamp'],
               'last_timestamp':selected[-1]['timestamp'],
               'median_posts_per_id':median(counts.values()), 'max_posts_per_id':max(counts.values()),
               'users_by_min_posts':{str(n):sum(c>=n for c in counts.values()) for n in (1,2,3,5,10,20)},
               'posts_by_min_posts':{str(n):sum(c for c in counts.values() if c>=n) for n in (1,2,3,5,10,20)},
               'top10_posts': [n for _,n in counts.most_common(10)]}
    save('summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
