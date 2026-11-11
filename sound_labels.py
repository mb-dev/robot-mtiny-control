"""Friendly labels for the robot's sound clips (our own names for vendor file paths). No hardware.

Sources, in order of trust: descriptive file names (pinyin translated by hand),
byte-identical copies of a named clip, pitch measured from the decoded audio
(note clips), and code ranges. Clips whose content is unknown get known=False so
the page can mark them. The clips themselves are never shipped; labels() reads
them from a folder you provide only to find byte-identical copies.
"""
import hashlib
import re
from pathlib import Path

PINYIN = dict(
    card_daku='Crying (card)', card_dazhaohu='Greeting (card)', card_gugujiao='Tummy rumble (card)',
    card_kaixin='Happy (card)', card_keshui='Sleepy (card)', card_lueluelue='Teasing "bleh" (card)',
    card_penti='Sneeze (card)', card_shengqi='Angry (card)', card_touyun='Dizzy (card)',
    card_xiayitiao='Startled (card)', code_bianchengkaishi='Coding: start programming',
    code_danbuzhixing='Coding: run one step', code_danbuzhixingwancheng='Coding: one step done',
    code_dianjikapian='Coding: tap a card', code_kaishizhixing='Coding: start running',
    code_tingzhidongzuo='Coding: stop', gen_cuowu='Wrong', gen_zhengque='Correct',
    gen_zhixingwancheng='Run finished', idle1_qiaqia='Idle: cha-cha tune', idle2_cradlesong='Idle: lullaby',
    idle3_snoring_male='Idle: snoring', volumn='Volume', a_o='Uh-oh', bing_go='Bingo', finished2='Finished',
    road_duang='Road bump', rule_bianchengkaishi='Rule: start programming', rule_daojishi='Rule: countdown',
    rule_jingdi='Rule: police siren', rule_jiuzhibingren='Rule: treat the patient',
    rule_saichehuosheng='Rule: race car wins', rule_saicheqidong='Rule: race car starts',
    rule_yisheng='Rule: doctor', rule_zhuaxiaotou='Rule: catch the thief', scene_dianxiangan='Scene: utility pole',
    scene_fish='Scene: fish', scene_huojing='Scene: fire alarm', scene_jiaoyou='Scene: picnic',
    scene_laodongzhe='Scene: worker', scene_meishijie='Scene: food street',
    scene_paizhaoyouke='Scene: tourist taking photos', scene_xiaotou='Scene: thief',
    game_ATM='Cash machine', game_beibao='Backpack', game_buxihuan="Don't like it", game_chidongxi='Eating',
    game_chilun='Gears', game_dakaisuo='Opening a lock', game_duzie='Hungry tummy', game_duziteng='Tummy ache',
    game_fangpi='Fart', game_fumianxiaoguo='Bad effect', game_guanqiashibai='Level failed', game_haiyang='Ocean',
    game_hengge='Humming', game_heshui='Drinking', game_huodeyingbi='Got a coin', game_jinqianliushui='Coins pouring',
    game_kaijinshumen='Metal door opens', game_kaisuo='Unlock', game_mutoupengzhuang='Wood bump',
    game_qingzhu='Celebration', game_shuaya='Brushing teeth', game_tebieyongli='Huge effort', game_tengtong='Ouch',
    game_tieqipengzhuang='Metal clang', game_xiemu='Curtain call', game_xingfen='Excited',
    game_yinqizhuyi='Attention', game_yongli='Effort', game_youxikaishi='Game start', game_youxituichu='Game exit',
    cat_muma='Cat rocking horse', fen='Points', earthwoom='Earthworm', icecreem_car='Ice cream car',
    volcanodes='Volcano', dinning='Dining room', start_excute_program='Start running the program')
CHROMATIC = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
WHITE = [7, 9, 11, 12, 14, 16, 17, 19, 21, 23, 24, 26, 28, 29]  # semitones above C3 for 0x84D..0x85A


def note(semitones_above_c3):
    return f'{CHROMATIC[semitones_above_c3 % 12]}{3 + semitones_above_c3 // 12}'


def code_label(folder, code):
    """Label for a number-coded clip, or None if its content is not known."""
    if 0x5635 <= code <= 0x564C:  # measured: chromatic run, 0x5635 = F2
        return f'Piano {note(code - 0x5635 - 7)}'
    if 0x84D <= code <= 0x85A:  # measured: white keys G3..F5
        return f'Note {note(WHITE[code - 0x84D])}'
    if 0xA01 <= code <= 0xA07 or 0xB01 <= code <= 0xB07:  # measured: C4..B4
        name = 'Do Re Mi Fa Sol La Ti'.split()[(code & 15) - 1]
        return f"{name} ({'long' if code < 0xB00 else 'short'})"
    return None


def labels(paths, root):
    """{path: dict(label, known)} for catalog paths (system/...); `root` is the folder that holds system/."""
    digest = {p: hashlib.sha1((Path(root) / p).read_bytes()).hexdigest() for p in paths}
    tones = {}  # the eight placeholder tones, lettered by first use among map-tile codes
    for i in range(1, 9):
        tones[digest[f'system/common/audio/0x400{i}.mp3']] = 'ABCDEFGH'[i - 1]
    named, out = {}, {}
    for p in paths:
        folder, stem = p[len('system/'):].rsplit('/', 1)[0], Path(p).stem
        if re.fullmatch(r'0x[0-9A-Fa-f]+', stem):
            continue
        match = re.fullmatch(r'(cat|dog|ghost)_0x([0-9A-Fa-f]{4})', stem)
        if match:
            label = f'{match[1].capitalize()} sings {note(int(match[2], 16) - 0x5635 - 7)}'
        elif folder == 'subject/language/english':
            label = f'Letter {stem.upper()}'
        elif stem.startswith('syllable_'):
            label = f'Syllable {stem[9:]}'
        else:
            label = PINYIN.get(stem) or stem.replace('_', ' ').capitalize()
        out[p] = dict(label=label, known=True)
        named.setdefault(digest[p], label)
    for p in paths:
        if p in out:
            continue
        folder, stem = p[len('system/'):].rsplit('/', 1)[0], Path(p).stem
        code = int(stem, 16)
        label = code_label(folder, code)
        if label is None and digest[p] in tones:
            where = 'map tile' if folder == 'common/audio' else 'guide'
            label = f'Tone {tones[digest[p]]} ({where} {code:X})'
        if label is None and digest[p] in named:
            label = f'{named[digest[p]]} (copy, {code:X})'
        if label is None:
            kind = 'Guide voice' if folder == 'game/guide' else 'Clip'
            out[p] = dict(label=f'{kind} {code:X}', known=False)
        else:
            out[p] = dict(label=label, known=True)
    return out
