#!/usr/bin/env python3
"""Preview overlapping faction positions without generating posts or calling APIs.

uv run --with matplotlib python scripts/plot_layout.py
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

from generate_data import GROUPS as CENTERS, POSITION_SIGMA, profiles

COLORS = {'A': '#43856f', 'B': '#bd6d58', 'C': '#7b78a8', 'D': '#c59e3e'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=50)
    parser.add_argument('--seed', type=int, default=20261017)
    parser.add_argument('--sigma', type=float, default=POSITION_SIGMA)
    parser.add_argument('--out', type=Path, default=Path('data/layout-preview-sigma045'))
    args = parser.parse_args()
    if not 4 <= args.size <= 50 or not 0 < args.sigma <= 2:
        parser.error('size must be 4..50 and sigma must be in (0, 2]')
    people = profiles(args.size, args.seed, args.sigma)
    args.out.mkdir(parents=True, exist_ok=True)
    counts = Counter(p['truth']['group'] for p in people)
    center_count = sum(abs(p['truth']['x']) < .3 and abs(p['truth']['y']) < .3 for p in people)
    crossing = sum(p['truth']['x']*CENTERS[p['truth']['group']][0] < 0 or
                   p['truth']['y']*CENTERS[p['truth']['group']][1] < 0 for p in people)
    fonts = {font.name for font in font_manager.fontManager.ttflist}
    japanese = next((f for f in ('Noto Sans CJK JP', 'Hiragino Sans', 'Hiragino Kaku Gothic ProN',
                                  'IPAexGothic', 'Yu Gothic') if f in fonts), None)
    if japanese:
        plt.rcParams['font.family'] = japanese
    plt.rcParams['axes.unicode_minus'] = False
    fig, ax = plt.subplots(figsize=(9, 9), layout='constrained')
    fig.patch.set_facecolor('#f5f6ef')
    ax.set_facecolor('#fbfcf8')
    ax.set_xlim(-1.05, 1.05)
    ax.set_ylim(-1.05, 1.05)
    ax.set_aspect('equal')
    ax.set_xticks([-1, -.5, 0, .5, 1])
    ax.set_yticks([-1, -.5, 0, .5, 1])
    ax.grid(color='#e1e6dc', linewidth=.7, zorder=0)
    ax.axhline(0, color='#aebdab', linewidth=1)
    ax.axvline(0, color='#aebdab', linewidth=1)
    for group, (cx, cy) in CENTERS.items():
        subset = [p['truth'] for p in people if p['truth']['group'] == group]
        ax.scatter([p['x'] for p in subset], [p['y'] for p in subset], s=115,
                   c=COLORS[group], alpha=.8, edgecolors='white', linewidths=1,
                   label=f'{group}  ({counts[group]})', zorder=3)
        ax.scatter([cx], [cy], marker='+', s=170, c=COLORS[group], linewidths=2.2, zorder=4)
    if japanese:
        ax.set_title(f'派閥の傾向と個人差から作る配置 · {args.size}人\n'
                     f'派閥中心 ±0.45 / 生成時の標準偏差 {args.sigma}', fontsize=17, pad=20)
        ax.set_xlabel('AI生成作品：禁止 ← → 容認', fontsize=12, labelpad=14)
        ax.set_ylabel('コミュニティの雰囲気：講評重視 ← → 気軽さ重視', fontsize=12, labelpad=14)
        caption = (f'＋：派閥の中心　｜　中央域（|x|, |y| < 0.3）：{center_count}人\n'
                   f'所属派閥と異なる象限：{crossing}人　｜　seed = {args.seed}\n'
                   '各軸を独立に正規分布から抽出。−1〜1の範囲外は引き直し。中間層の別枠なし。')
    else:
        ax.set_title(f'Overlapping opinions · {args.size} members\n'
                     f'Faction centers ±0.45 / sampling SD {args.sigma}', fontsize=17, pad=20)
        ax.set_xlabel('AI-generated artwork: ban  ← →  welcome', fontsize=12, labelpad=14)
        ax.set_ylabel('Community atmosphere: critique  ← →  casual', fontsize=12, labelpad=14)
        caption = (f'+ = faction center  |  Central area (|x|, |y| < 0.3): {center_count} members\n'
                   f'Outside own faction quadrant: {crossing} members  |  seed = {args.seed}\n'
                   'Independent normal draws per axis; reject outside [-1, 1]. No separate moderate group.')
    ax.legend(title='Faction / members', loc='upper left', framealpha=.9, fontsize=10)
    fig.supxlabel(caption, fontsize=10, color='#526553')
    for spine in ax.spines.values():
        spine.set_color('#c3cdbd')
    stem = f'community-{args.size}-sigma-{args.sigma}'
    for extension in ('png', 'svg'):
        fig.savefig(args.out/f'{stem}.{extension}', dpi=170)
    plt.close(fig)
    manifest = {'size': args.size, 'seed': args.seed, 'sigma_before_truncation': args.sigma,
                'centers': CENTERS, 'sampling': 'independent truncated normal by rejection on [-1,1]',
                'central_members': center_count, 'outside_own_quadrant': crossing, 'profiles': people}
    (args.out/f'{stem}.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'plot': str(args.out/f'{stem}.png'), 'members': len(people),
                      'groups': dict(counts), 'central_members': center_count,
                      'outside_own_quadrant': crossing, 'japanese_font': japanese}, ensure_ascii=False))


if __name__ == '__main__':
    main()
