#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
多音字读音客观核验器。

背景：Edge 免费 TTS 端点不接受 <phoneme>（实测 sapi/pinyin/ups/ipa 四种
alphabet 全部返回 NoAudioReceived，只有纯文本 SSML 能通过），所以无法强制注音。
可行手段是"同音字诱导"：audio.py 原生支持 spokenText 与 text 分离——
spokenText 写成同音字强制读音，text 保持正确汉字用于字幕与工程显示。

本脚本回答一个问题：TTS 实际把某个多音字读成了哪个音？
做法：合成"待测词"与两个"诱导参照词"（各对应一种读音的普通汉字），
计算 log-mel 谱的 DTW 距离，哪个参照更近就是哪个音。

用法：
    python polyphone_check.py <待测词> <参照A:读音1> <参照B:读音2> [更多参照...]
例：
    python polyphone_check.py 测量 测良 测亮

全稿扫查（探针表机制，移植自「陪你夜读」团队 polyphone_probe / 审稿关卡3）：
    python polyphone_check.py --scan narration.json
    python polyphone_check.py --scan 文稿.txt
    python polyphone_check.py --scan --text "这一行是对的"
扫 polyphone_probes.json 里的全部多音字，逐处输出上下文、两音、典型搭配与探针字，
供人工定「本处应读音」；定完音再用上面的单词模式做 DTW 客观核验。
"""
import asyncio, json, os, subprocess, sys, tempfile
from pathlib import Path
import numpy as np

VOICE = 'zh-CN-XiaoxiaoNeural'
SR = 48000
PROBES_PATH = Path(__file__).with_name('polyphone_probes.json')


def load_probes():
    try:
        return json.loads(PROBES_PATH.read_text(encoding='utf-8'))['chars']
    except Exception:
        return {}


async def synth(word, path):
    import edge_tts
    await edge_tts.Communicate(word, VOICE).save(path)
    return path


def decode(mp3):
    wav = mp3.replace('.mp3', '.wav')
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', mp3, '-ac', '1', '-ar', str(SR), '-c:a', 'pcm_s16le', wav],
                   check=True)
    import wave
    w = wave.open(wav)
    a = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float64) / 32768.0
    return a


def logmel(x, n_fft=1200, hop=480, n_mels=40):
    """手动 mel 谱：无 scipy/librosa 依赖。"""
    win = np.hanning(n_fft)
    frames = [x[i:i + n_fft] * win for i in range(0, max(1, len(x) - n_fft), hop)]
    if not frames:
        return np.zeros((1, n_mels))
    S = np.abs(np.fft.rfft(np.array(frames), n=n_fft))
    fb = mel_filterbank(n_mels, n_fft // 2 + 1, SR)
    M = S @ fb.T
    return np.log(M + 1e-8)


def mel_filterbank(n_mels, n_bins, sr, fmin=80.0, fmax=7600.0):
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    bins = np.floor((n_bins * 2) * pts / sr).astype(int)
    fb = np.zeros((n_mels, n_bins))
    for i in range(n_mels):
        l, c, r = bins[i], bins[i + 1], bins[i + 2]
        if c == l:
            c = l + 1
        if r <= c:
            r = c + 1
        fb[i, l:c] = (np.arange(l, c) - l) / (c - l)
        fb[i, c:r] = (r - np.arange(c, r)) / (r - c)
    return np.clip(fb, 0, None)


def dtw(a, b):
    """DTW 距离（归一化到对齐路径长度）。"""
    n, m = len(a), len(b)
    D = np.full((n + 1, m + 1), np.inf)
    D[0, 0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            c = np.sqrt(np.sum((a[i - 1] - b[j - 1]) ** 2))
            D[i, j] = c + min(D[i - 1, j], D[i, j - 1], D[i - 1, j - 1])
    # 回溯取路径长度
    i, j, k = n, m, 0
    while i > 0 and j > 0:
        k += 1
        i, j = min(((i - 1, j), (i, j - 1), (i - 1, j - 1)), key=lambda p: D[p[0], p[1]])
    return D[n, m] / max(k, 1)


def scan_texts(texts, probes):
    """texts: [(标签, 句子)]。输出逐处上下文清单。"""
    rows, n_chars = [], 0
    for tag, text in texts:
        for i, ch in enumerate(text):
            if ch not in probes:
                continue
            n_chars += 1
            info = probes[ch]
            lo, hi = max(0, i - 12), min(len(text), i + 13)
            ctx = text[lo:i] + '【' + ch + '】' + text[i + 1:hi]
            readings = ' ｜ '.join(
                '%s(%s%s)' % (r['note'], r['py'],
                              ' 探针%s' % r['probe'] if r.get('probe') else '')
                + (' 搭配:%s' % '/'.join(r['coll'][:3]) if r.get('coll') else '')
                for r in info)
            rows.append((tag, ch, ctx, readings))
    return rows, n_chars


def collect_texts(arg):
    """从 narration.json / txt / --text 收集 [(标签, 句子)]。"""
    if arg == '--text':
        return [('--text', sys.argv[sys.argv.index('--text') + 1])]
    p = Path(arg)
    if not p.exists():
        print('读不了文件：%s' % arg)
        sys.exit(2)
    raw = p.read_text(encoding='utf-8')
    if raw.lstrip().startswith('{') and 'segments' in raw:
        segs = json.loads(raw)['segments']
        return [('%s' % s.get('id', i), s.get('text', '')) for i, s in enumerate(segs)]
    return [('%s:%d' % (p.name, i), ln.strip()) for i, ln in enumerate(raw.splitlines()) if ln.strip()]


async def run_scan(arg):
    probes = load_probes()
    if not probes:
        print('探针表缺失：%s' % PROBES_PATH)
        sys.exit(2)
    texts = collect_texts(arg)
    rows, n = scan_texts(texts, probes)
    print('全稿多音字扫查：共 %d 处（探针表 %d 字种）' % (n, len(probes)))
    for tag, ch, ctx, readings in rows:
        print('\n[%s] %s' % (tag, ctx))
        print('    %s' % readings)
    print('\n=== 下一步（夜读关卡3 流程）===')
    print('1) 逐处定「本处应读音」：搭配语命中哪个音就是哪个音；拿不准的标存疑。')
    print('2) 读音歧义消除不了的：改稿（换等义词 / 改结构 / 拆行），别留给配音阶段。')
    print('3) 定稿后对存疑处做客观核验：python polyphone_check.py 待测词 探针A探针句参照 探针B探针句参照')
    print('   （把探针字放进同款句式里当参照词，DTW 判 TTS 实际读音）')


async def main():
    args = sys.argv[1:]
    if args and args[0] == '--scan':
        if len(args) < 2:
            print(__doc__)
            sys.exit(2)
        await run_scan(args[1])
        return
    if len(args) < 3:
        print(__doc__)
        sys.exit(2)
    target, refs = args[0], args[1:]
    tmp = tempfile.mkdtemp(prefix='polyphone_')
    words = [target] + refs
    feats = {}
    for w in words:
        mp3 = os.path.join(tmp, '%s.mp3' % abs(hash(w)))
        await synth(w, mp3)
        feats[w] = logmel(decode(mp3))
        print('  合成 %-8s %d 帧' % (w, len(feats[w])))
    print('\n待测词: %s' % target)
    res = []
    for r in refs:
        d = dtw(feats[target], feats[r])
        res.append((d, r))
        print('  DTW(%s, %s) = %.4f' % (target, r, d))
    res.sort()
    print('\n判定: %s 的实际读音最接近参照「%s」(dist=%.4f)，次近「%s」(dist=%.4f)'
          % (target, res[0][1], res[0][0], res[1][1], res[1][0]))
    print('提示: 若最接近的参照不是你期望的读音，用 spokenText 写同音字诱导，text 保留正确汉字。')


if __name__ == '__main__':
    asyncio.run(main())
