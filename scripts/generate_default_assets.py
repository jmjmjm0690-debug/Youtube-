"""
坂本アヒル様 公式PSD立ち絵素材（四国めたん＆ずんだもん）自動展開＆BGM生成スクリプト
(scripts/generate_default_assets.py)

ニコニコ静画 (im10788496 / im10791276) の公式配布先 (tachie.net) から
坂本アヒル様の「ずんだもん立ち絵素材2.3.psd」および「四国めたん立ち絵素材2.1.psd」を取得し、
`psd-tools` で眉・目・口（口閉じ/口開き）・腕ポーズのレイヤーを正確に切り替えて、
全5種類の感情差分（normal / happy / surprised / serious / troubled）× 口パク開閉（close / open）
の計20枚の高解像度透過PNGを `assets/characters/metan/` と `assets/characters/zundamon/` に書き出します。
"""

import io
import logging
import math
import struct
import wave
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

import httpx
from PIL import Image, ImageOps
from psd_tools import PSDImage

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
EMOTIONS = ["normal", "happy", "surprised", "serious", "troubled"]
MOUTH_STATES = ["close", "open"]

# 坂本アヒル様 公式配布ページ (tachie.net) ID
SAKAMOTO_TACHIE_IDS = {
    "zundamon": "42HbZ2d4sf",  # im10788496 ずんだもん立ち絵素材2.3
    "metan": "4R0KurbGBy",     # im10791276 四国めたん立ち絵素材2.1
}


def _set_group_choice(group, active_names: List[str]) -> None:
    for layer in group:
        layer.visible = layer.name in active_names


def _ensure_psd_file(char_id: str, cache_dir: Path) -> Path:
    """tachie.net から坂本アヒル様の公式PSD立ち絵ZIPをダウンロードしてPSDを取り出す"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    psd_path = cache_dir / f"{char_id}.psd"
    if psd_path.exists() and psd_path.stat().st_size > 1_000_000:
        return psd_path

    wid = SAKAMOTO_TACHIE_IDS[char_id]
    page_url = f"https://tachie.net/w/{wid}"
    dl_url = f"https://tachie.net/w/{wid}/download"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
    }
    with httpx.Client(
        follow_redirects=True, timeout=60.0, headers=headers
    ) as client:
        client.get(page_url)
        resp = client.get(dl_url, headers={"Referer": page_url})
        resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for info in zf.infolist():
            if info.filename.lower().endswith(".psd"):
                psd_path.write_bytes(zf.read(info.filename))
                return psd_path

    raise RuntimeError(f"PSDファイルがZIP内に見つかりませんでした: {char_id}")


def _configure_metan_layers(psd: PSDImage, emotion: str, mouth: str) -> None:
    """
    四国めたん立ち絵素材2.1.psd のレイヤーを感情×口パク状態に応じて切り替える。
    ※画面左側に配置するため、書き出し後に左右反転して右側（ずんだもん側）を向かせる。
    """
    # 感情別レイヤー定義: (眉, 白目, 黒目, 口閉じ, 口開き, 右腕, 左腕, 顔色, 汗)
    presets: Dict[str, Tuple[str, str, str, str, str, str, str, str, bool]] = {
        "normal": (
            "*ごきげん",
            "*普通白目",
            "*普通目",
            "*ほほえみ",
            "*わあー",
            "*手をかざす",
            "*普通",
            "*普通2",
            False,
        ),
        "happy": (
            "*ごきげん",
            "*普通白目",
            "*普通目",
            "*ほほえみ",
            "*▽",
            "*手をかざす",
            "*普通",
            "*赤面",
            False,
        ),
        "surprised": (
            "*こまり",
            "*見開き白目",
            "*普通目2",
            "*お",
            "*わあー",
            "*手をかざす",
            "*口元に指",
            "*普通2",
            True,
        ),
        "serious": (
            "*ややおこ",
            "*普通白目",
            "*普通目",
            "*む",
            "*お",
            "*指差す",
            "*普通",
            "*普通2",
            False,
        ),
        "troubled": (
            "*こまり",
            "*普通白目",
            "*目そらし",
            "*んー",
            "*うえー",
            "*普通",
            "*口元に指",
            "*青ざめ",
            True,
        ),
    }
    brow, sclera, pupil, m_close, m_open, r_arm, l_arm, cheek, sweat = (
        presets.get(emotion, presets["normal"])
    )
    active_mouth = m_open if mouth == "open" else m_close

    for layer in psd:
        if layer.name == "回転移動でポニテにするなど":
            layer.visible = False
        elif layer.name in ("ツインドリル右", "ツインドリル左", "!前髪もみあげ"):
            layer.visible = True
        elif layer.name == "*その他の服":
            layer.visible = False
        elif layer.name == "*白ロリ服":
            layer.visible = True
            for sub in layer:
                if sub.name == "!体":
                    sub.visible = True
                elif sub.name == "!右腕":
                    sub.visible = True
                    _set_group_choice(sub, [r_arm])
                elif sub.name == "!左腕":
                    sub.visible = True
                    _set_group_choice(sub, [l_arm])
        elif layer.name == "!顔色":
            layer.visible = True
            _set_group_choice(layer, [cheek])
        elif layer.name == "!口":
            layer.visible = True
            _set_group_choice(layer, [active_mouth])
        elif layer.name == "!眉":
            layer.visible = True
            _set_group_choice(layer, [brow])
        elif layer.name == "!目":
            layer.visible = True
            if emotion == "happy" and mouth == "close":
                _set_group_choice(layer, ["*目閉じ2"])
            else:
                _set_group_choice(layer, ["*目セット"])
                for sub in layer:
                    if sub.name == "*目セット":
                        sub.visible = True
                        for s2 in sub:
                            if s2.name in ("*普通白目", "*見開き白目"):
                                s2.visible = s2.name == sclera
                            elif s2.name == "!黒目":
                                s2.visible = True
                                _set_group_choice(s2, [pupil])
        elif layer.name == "頭部アクセサリ":
            layer.visible = True
            _set_group_choice(layer, ["髪留めハート", "ヘッドドレス"])
        elif layer.name == "記号など":
            layer.visible = True
            _set_group_choice(layer, ["汗"] if sweat else [])


def _configure_zundamon_layers(
    psd: PSDImage, emotion: str, mouth: str
) -> None:
    """
    ずんだもん立ち絵素材2.3.psd のレイヤーを感情×口パク状態に応じて切り替える。
    ※画面右側に配置するため、左向き（めたん側を向くデフォルト向き）のまま書き出す。
    """
    # 感情別レイヤー定義: (眉, 目モード, 白目, 黒目, 口閉じ, 口開き, 左腕, 右腕, 枝豆, 汗)
    presets: Dict[
        str, Tuple[str, str, str, str, str, str, str, str, str, bool]
    ] = {
        "normal": (
            "*普通眉",
            "*目セット",
            "*普通白目",
            "*普通目",
            "*むふ",
            "*ほあー",
            "*基本",
            "*基本",
            "*枝豆通常",
            False,
        ),
        "happy": (
            "*普通眉",
            "*にっこり",
            "*普通白目",
            "*普通目",
            "*むふ",
            "*ほあ",
            "*手を挙げる",
            "*手を挙げる",
            "*枝豆通常",
            False,
        ),
        "surprised": (
            "*上がり眉",
            "*目セット",
            "*見開き白目",
            "*普通目2",
            "*お",
            "*ほあー",
            "*口元",
            "*手を挙げる",
            "*枝豆通常",
            True,
        ),
        "serious": (
            "*怒り眉",
            "*目セット",
            "*普通白目",
            "*普通目",
            "*むー",
            "*ほー",
            "*考える",
            "*指差し",
            "*枝豆通常",
            False,
        ),
        "troubled": (
            "*困り眉1",
            "*目セット",
            "*普通白目",
            "*普通目",
            "*んー",
            "*んへー",
            "*考える",
            "*基本",
            "*枝豆萎え",
            True,
        ),
    }
    (
        brow,
        eye_mode,
        sclera,
        pupil,
        m_close,
        m_open,
        l_arm,
        r_arm,
        edamame,
        sweat,
    ) = presets.get(emotion, presets["normal"])
    active_mouth = m_open if mouth == "open" else m_close

    for layer in psd:
        if layer.name == "尻尾的なアレ":
            layer.visible = True
        elif layer.name == "パーカー裏地":
            layer.visible = False
        elif layer.name == "*服装2":
            layer.visible = False
        elif layer.name == "*服装1":
            layer.visible = True
            for sub in layer:
                if sub.name == "*制服":
                    sub.visible = False
                elif sub.name == "*いつもの服":
                    sub.visible = True
                elif sub.name == "!左腕":
                    sub.visible = True
                    _set_group_choice(sub, [l_arm])
                elif sub.name == "!右腕":
                    sub.visible = True
                    _set_group_choice(sub, [r_arm])
        elif layer.name == "!口":
            layer.visible = True
            _set_group_choice(layer, [active_mouth])
        elif layer.name == "!顔色":
            layer.visible = True
            _set_group_choice(
                layer,
                ["*ほっぺ", "かげり"] if emotion == "troubled" else ["*ほっぺ"],
            )
        elif layer.name == "!目":
            layer.visible = True
            _set_group_choice(layer, [eye_mode])
            if eye_mode == "*目セット":
                for sub in layer:
                    if sub.name == "*目セット":
                        sub.visible = True
                        for s2 in sub:
                            if s2.name in ("*普通白目", "*見開き白目", "*ジト白目"):
                                s2.visible = s2.name == sclera
                            elif s2.name == "!黒目":
                                s2.visible = True
                                _set_group_choice(s2, [pupil])
        elif layer.name == "!眉":
            layer.visible = True
            _set_group_choice(layer, [brow])
        elif layer.name == "!枝豆":
            layer.visible = True
            _set_group_choice(layer, [edamame])
        elif layer.name == "記号など":
            layer.visible = True
            _set_group_choice(layer, ["汗1"] if sweat else [])


def build_sakamoto_character_assets(
    root_dir: Path = ROOT_DIR, force_overwrite: bool = False
) -> None:
    """坂本アヒル様のPSDから四国めたん・ずんだもんの全5感情×口パク開閉PNGを生成"""
    metan_dir = root_dir / "assets" / "characters" / "metan"
    zunda_dir = root_dir / "assets" / "characters" / "zundamon"
    metan_dir.mkdir(parents=True, exist_ok=True)
    zunda_dir.mkdir(parents=True, exist_ok=True)

    if not force_overwrite:
        all_exist = all(
            (metan_dir / f"{e}_{m}.png").exists()
            and (zunda_dir / f"{e}_{m}.png").exists()
            for e in EMOTIONS
            for m in MOUTH_STATES
        )
        if all_exist:
            return

    cache_dir = root_dir / "tmp" / "sakamoto"
    metan_psd_path = _ensure_psd_file("metan", cache_dir)
    zunda_psd_path = _ensure_psd_file("zundamon", cache_dir)

    # 1. 四国めたん (1082x1820 -> 腰上クロップ & 左右反転して右向きに配置)
    metan_psd = PSDImage.open(metan_psd_path)
    for emotion in EMOTIONS:
        for mouth in MOUTH_STATES:
            out_path = metan_dir / f"{emotion}_{mouth}.png"
            if out_path.exists() and not force_overwrite:
                continue
            _configure_metan_layers(metan_psd, emotion, mouth)
            comp = metan_psd.composite(force=True)
            mirrored = ImageOps.mirror(comp)
            cropped = mirrored.crop((60, 40, 1020, 1340))
            resized = cropped.resize((576, 780), Image.Resampling.LANCZOS)
            resized.save(out_path, "PNG")

    # 2. ずんだもん (1082x1650 -> 腰上クロップ & 左向きのまま配置)
    zunda_psd = PSDImage.open(zunda_psd_path)
    for emotion in EMOTIONS:
        for mouth in MOUTH_STATES:
            out_path = zunda_dir / f"{emotion}_{mouth}.png"
            if out_path.exists() and not force_overwrite:
                continue
            _configure_zundamon_layers(zunda_psd, emotion, mouth)
            comp = zunda_psd.composite(force=True)
            cropped = comp.crop((60, 40, 1020, 1340))
            resized = cropped.resize((576, 780), Image.Resampling.LANCZOS)
            resized.save(out_path, "PNG")


def generate_default_bgm(
    bgm_path: Path, duration_sec: int = 16, sample_rate: int = 24000
) -> None:
    """著作権フリーの心地よいアンビエント・ローファイ風コード進行ループBGM(WAV)を自動合成"""
    if bgm_path.exists():
        return

    bgm_path.parent.mkdir(parents=True, exist_ok=True)
    total_samples = duration_sec * sample_rate

    chords: Tuple[Tuple[float, ...], ...] = (
        (174.61, 220.00, 261.63, 329.63),  # Fmaj7
        (196.00, 246.94, 293.66, 349.23),  # G7
        (164.81, 196.00, 246.94, 293.66),  # Em7
        (220.00, 261.63, 329.63, 392.00),  # Am7
    )

    frames = bytearray()
    chord_duration = duration_sec / len(chords)

    for i in range(total_samples):
        t = i / sample_rate
        chord_idx = int(t // chord_duration) % len(chords)
        local_t = t % chord_duration
        freqs = chords[chord_idx]

        env = 1.0
        if local_t < 0.25:
            env = local_t / 0.25
        elif local_t > chord_duration - 0.35:
            env = (chord_duration - local_t) / 0.35

        signal = 0.0
        for f in freqs:
            signal += math.sin(2.0 * math.pi * f * t) * 0.20
            signal += math.sin(2.0 * math.pi * (f * 2.0) * t) * 0.04

        arp_idx = int((t * 2) % len(freqs))
        arp_env = max(0.0, 1.0 - ((t * 2) % 1.0) * 1.4)
        signal += (
            math.sin(2.0 * math.pi * (freqs[arp_idx] * 2.0) * t) * 0.08 * arp_env
        )

        val = int(max(-1.0, min(1.0, signal * env * 0.35)) * 32767)
        frames.extend(struct.pack("<h", val))

    with wave.open(str(bgm_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))


def ensure_default_assets(
    root_dir: Path = ROOT_DIR, force_overwrite: bool = False
) -> None:
    """坂本アヒル様の公式PSD立ち絵5感情×口パク開閉アセットおよびBGMを準備する"""
    build_sakamoto_character_assets(
        root_dir=root_dir, force_overwrite=force_overwrite
    )
    bgm_path = root_dir / "assets" / "bgm" / "default_loop.wav"
    if not bgm_path.exists():
        generate_default_bgm(bgm_path)


if __name__ == "__main__":
    ensure_default_assets(force_overwrite=True)
    print(
        "坂本アヒル様の公式PSD立ち絵（四国めたん＆ずんだもん 全5感情×口パク開閉 計20枚）の展開が完了しました。"
    )
