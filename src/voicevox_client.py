import io
import logging
import math
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import AppConfig
from src.script_generator import GeneratedEpisode, SlideCard

logger = logging.getLogger(__name__)


@dataclass
class SynthesizedSegment:
    """1セリフ分の音声合成結果およびタイムライン情報"""

    global_index: int
    section_id: str
    section_title: str
    section_index: int
    speaker: str  # "metan" or "zundamon"
    speaker_id: int  # 2 or 3
    emotion: str
    display_text: str
    spoken_text: str
    slide: SlideCard
    wav_path: Path
    speech_duration_sec: float
    pause_duration_sec: float
    total_duration_sec: float
    start_time_sec: float
    end_time_sec: float


@dataclass
class AudioTimelineResult:
    """エピソード全体の音声合成結果とチャプタータイムスタンプ"""

    segments: List[SynthesizedSegment]
    master_voice_wav_path: Path
    total_duration_sec: float
    section_timestamps: Dict[str, str]


class VoicevoxClient:
    """
    VOICEVOX Engine REST API (Docker / デスクトップ版) または
    ローカル組み込みの VOICEVOX 公式コアエンジン (`voicevox_core` + `0.vvm`) と連携し、
    本物の四国めたん(ノーマル ID:2)とずんだもん(ノーマル ID:3)の音声合成を行うクライアント。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base_url = config.env.voicevox_base_url
        self.vv_cfg = config.settings.get("voicevox", {})
        self.sample_rate = int(
            config.settings.get("video", {}).get("sample_rate", 24000)
        )
        self.inter_line_pause = float(
            self.vv_cfg.get("inter_line_pause_sec", 0.24)
        )
        self.section_pause = float(self.vv_cfg.get("section_pause_sec", 0.75))
        self.characters_cfg = self.vv_cfg.get("characters", {})
        self.surface_replacements: Dict[str, str] = (
            config.pronunciation_dict.get("surface_replacements", {}) or {}
        )
        self.user_dict_words: List[Dict] = (
            config.pronunciation_dict.get("user_dict_words", []) or []
        )
        self._core_synthesizer: Any = None

    def check_health(self, wait_seconds: int = 10) -> bool:
        """VOICEVOX Engine REST API (:50021) の起動を確認する"""
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(f"{self.base_url}/version")
                    if resp.status_code == 200:
                        logger.info(
                            "VOICEVOX Engine REST API 接続確認OK (version=%s, url=%s)",
                            resp.text.strip(),
                            self.base_url,
                        )
                        return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def _init_local_voicevox_core(self) -> bool:
        """
        REST API コンテナが未起動の環境（Windowsローカル実行等）で、
        公式 `voicevox_core` + `0.vvm` を直接ロードして本物の四国めたん(ID:2)・ずんだもん(ID:3)音声を合成する。
        """
        if self._core_synthesizer is not None:
            return True

        core_dir = self.config.root_dir / "voicevox_core"
        vvm_path = core_dir / "models" / "vvms" / "0.vvm"
        dict_dir = core_dir / "dict" / "open_jtalk_dic_utf_8-1.11"
        ort_candidates = [
            core_dir / "onnxruntime" / "lib" / "voicevox_onnxruntime.dll",
            core_dir / "onnxruntime" / "lib" / "libvoicevox_onnxruntime.so",
            core_dir / "onnxruntime" / "lib" / "libvoicevox_onnxruntime.dylib",
        ]
        ort_path = next((p for p in ort_candidates if p.exists()), None)

        if not (vvm_path.exists() and dict_dir.exists() and ort_path):
            return False

        try:
            from voicevox_core.blocking import (
                Onnxruntime,
                OpenJtalk,
                Synthesizer,
                VoiceModelFile,
            )

            ort = Onnxruntime.load_once(filename=str(ort_path))
            ojt = OpenJtalk(str(dict_dir))
            synth = Synthesizer(ort, ojt)
            with VoiceModelFile.open(str(vvm_path)) as model:
                synth.load_voice_model(model)

            self._core_synthesizer = synth
            logger.info(
                "VOICEVOX 公式コアエンジン (voicevox_core + 0.vvm) の直接ロードに成功しました（本物の四国めたん・ずんだもん音声を使用）"
            )
            return True
        except Exception as e:
            logger.warning("voicevox_core の初期化に失敗しました: %s", e)
            return False

    def register_user_dictionary(self) -> None:
        """config/pronunciation_dict.yaml のカスタム単語を VOICEVOX Engine /user_dict に登録する"""
        if not self.user_dict_words:
            return
        try:
            with httpx.Client(timeout=10.0) as client:
                existing_resp = client.get(f"{self.base_url}/user_dict")
                existing_surfaces = set()
                if existing_resp.status_code == 200:
                    for item in existing_resp.json().values():
                        existing_surfaces.add(item.get("surface", ""))

                for word in self.user_dict_words:
                    surface = word.get("surface", "")
                    if not surface or surface in existing_surfaces:
                        continue
                    params = {
                        "surface": surface,
                        "pronunciation": word.get("pronunciation", ""),
                        "accent_type": int(word.get("accent_type", 1)),
                        "word_type": word.get("word_type", "PROPER_NOUN"),
                        "priority": int(word.get("priority", 7)),
                    }
                    client.post(f"{self.base_url}/user_dict_word", params=params)
            logger.info("VOICEVOX カスタム発音辞書の同期が完了しました")
        except Exception as e:
            logger.warning("VOICEVOX ユーザー辞書登録をスキップしました: %s", e)

    def preprocess_spoken_text(self, text: str) -> str:
        """字幕表示用の元テキストは維持したまま、音声合成用の読み仮名置換を適用する"""
        spoken = text
        for src, dst in sorted(
            self.surface_replacements.items(), key=lambda x: len(x[0]), reverse=True
        ):
            spoken = spoken.replace(src, dst)
        return spoken

    def _synthesize_with_local_core(
        self, spoken_text: str, speaker_key: str
    ) -> bytes:
        """ローカル組み込みの公式 voicevox_core (0.vvm) で本物の音声WAVを合成"""
        char_cfg = self.characters_cfg.get(speaker_key, {})
        speaker_id = int(
            char_cfg.get("speaker_id", 2 if speaker_key == "metan" else 3)
        )
        speed_scale = float(char_cfg.get("speed_scale", 1.18))
        pitch_scale = float(char_cfg.get("pitch_scale", 0.0))
        intonation_scale = float(char_cfg.get("intonation_scale", 1.08))
        volume_scale = float(char_cfg.get("volume_scale", 1.0))

        query = self._core_synthesizer.create_audio_query(
            spoken_text, speaker_id
        )
        query.speed_scale = speed_scale
        query.pitch_scale = pitch_scale
        query.intonation_scale = intonation_scale
        query.volume_scale = volume_scale
        query.output_sampling_rate = self.sample_rate
        query.output_stereo = False
        query.pre_phoneme_length = 0.05
        query.post_phoneme_length = 0.05

        return self._core_synthesizer.synthesis(query, speaker_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def _synthesize_raw_wav(self, spoken_text: str, speaker_key: str) -> bytes:
        char_cfg = self.characters_cfg.get(speaker_key, {})
        speaker_id = int(
            char_cfg.get("speaker_id", 2 if speaker_key == "metan" else 3)
        )
        speed_scale = float(char_cfg.get("speed_scale", 1.18))
        pitch_scale = float(char_cfg.get("pitch_scale", 0.0))
        intonation_scale = float(char_cfg.get("intonation_scale", 1.08))
        volume_scale = float(char_cfg.get("volume_scale", 1.0))

        with httpx.Client(timeout=45.0) as client:
            q_resp = client.post(
                f"{self.base_url}/audio_query",
                params={"text": spoken_text, "speaker": speaker_id},
            )
            q_resp.raise_for_status()
            query_data = q_resp.json()

            query_data["speedScale"] = speed_scale
            query_data["pitchScale"] = pitch_scale
            query_data["intonationScale"] = intonation_scale
            query_data["volumeScale"] = volume_scale
            query_data["outputSamplingRate"] = self.sample_rate
            query_data["outputStereo"] = False
            query_data["prePhonemeLength"] = 0.05
            query_data["postPhonemeLength"] = 0.05

            s_resp = client.post(
                f"{self.base_url}/synthesis",
                params={"speaker": speaker_id},
                json=query_data,
            )
            s_resp.raise_for_status()
            return s_resp.content

    def _append_silence_and_save(
        self, raw_wav_bytes: bytes, pause_sec: float, out_path: Path
    ) -> Tuple[float, float]:
        """合成されたWAVデータの末尾に指定秒数の無音ポーズを付加して保存"""
        with wave.open(io.BytesIO(raw_wav_bytes), "rb") as rf:
            n_channels = rf.getnchannels()
            sampwidth = rf.getsampwidth()
            framerate = rf.getframerate()
            n_frames = rf.getnframes()
            audio_frames = rf.readframes(n_frames)

        speech_duration = n_frames / float(framerate)
        pause_frames_count = int(pause_sec * framerate)
        silence_bytes = b"\x00" * (pause_frames_count * n_channels * sampwidth)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(n_channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(framerate)
            wf.writeframes(audio_frames + silence_bytes)

        total_duration = (n_frames + pause_frames_count) / float(framerate)
        return speech_duration, total_duration

    @staticmethod
    def _format_mmss(seconds: float) -> str:
        total_sec = max(0, int(seconds))
        mm = total_sec // 60
        ss = total_sec % 60
        return f"{mm:02d}:{ss:02d}"

    def synthesize_episode(
        self,
        episode: GeneratedEpisode,
        work_dir: Path,
        allow_offline_fallback: bool = False,
    ) -> AudioTimelineResult:
        """
        台本全体の全セリフを本物の VOICEVOX (REST API または ローカル voicevox_core) で音声合成する。
        """
        audio_dir = work_dir / "audio_segments"
        audio_dir.mkdir(parents=True, exist_ok=True)

        use_http_engine = self.check_health(wait_seconds=3)
        use_local_core = False

        if use_http_engine:
            self.register_user_dictionary()
        else:
            use_local_core = self._init_local_voicevox_core()
            if not use_local_core:
                raise RuntimeError(
                    f"VOICEVOX Engine ({self.base_url}) およびローカル voicevox_core のいずれも利用できません。"
                )

        segments: List[SynthesizedSegment] = []
        section_timestamps: Dict[str, str] = {}
        current_time_sec = 0.0
        global_idx = 0

        active_slide = SlideCard(
            slide_title=episode.topic_keyword[:24],
            diagram_type="bullet_points",
            slide_points=["急上昇トレンドの背景と独自考察"],
            highlight_keyword="徹底解説",
        )

        for sec_idx, section in enumerate(episode.sections):
            section_timestamps[section.section_id] = (
                "00:00"
                if sec_idx == 0
                else self._format_mmss(current_time_sec)
            )

            for line_idx, line in enumerate(section.lines):
                global_idx += 1
                if line.slide is not None:
                    active_slide = line.slide

                spoken_text = self.preprocess_spoken_text(line.text)
                is_last_in_section = line_idx == len(section.lines) - 1
                pause_sec = (
                    self.section_pause
                    if is_last_in_section
                    else self.inter_line_pause
                )

                char_cfg = self.characters_cfg.get(line.speaker, {})
                speaker_id = int(
                    char_cfg.get(
                        "speaker_id", 2 if line.speaker == "metan" else 3
                    )
                )

                if use_http_engine:
                    raw_wav = self._synthesize_raw_wav(spoken_text, line.speaker)
                else:
                    raw_wav = self._synthesize_with_local_core(
                        spoken_text, line.speaker
                    )

                seg_wav_path = audio_dir / f"seg_{global_idx:04d}.wav"
                speech_dur, total_dur = self._append_silence_and_save(
                    raw_wav, pause_sec, seg_wav_path
                )

                start_t = current_time_sec
                end_t = start_t + total_dur
                current_time_sec = end_t

                segments.append(
                    SynthesizedSegment(
                        global_index=global_idx,
                        section_id=section.section_id,
                        section_title=section.section_title,
                        section_index=sec_idx,
                        speaker=line.speaker,
                        speaker_id=speaker_id,
                        emotion=line.emotion,
                        display_text=line.text,
                        spoken_text=spoken_text,
                        slide=active_slide,
                        wav_path=seg_wav_path,
                        speech_duration_sec=speech_dur,
                        pause_duration_sec=pause_sec,
                        total_duration_sec=total_dur,
                        start_time_sec=start_t,
                        end_time_sec=end_t,
                    )
                )

        master_wav_path = work_dir / "master_voice.wav"
        self._concatenate_wavs(
            [s.wav_path for s in segments], master_wav_path
        )

        logger.info(
            "全 %d セリフの本物VOICEVOX音声合成完了: 実測総再生時間 = %.1f秒 (%.2f分)",
            len(segments),
            current_time_sec,
            current_time_sec / 60.0,
        )

        return AudioTimelineResult(
            segments=segments,
            master_voice_wav_path=master_wav_path,
            total_duration_sec=current_time_sec,
            section_timestamps=section_timestamps,
        )

    def _concatenate_wavs(
        self, wav_paths: List[Path], master_out_path: Path
    ) -> None:
        """全セリフのWAVファイルを単一のマスターナレーションWAVファイルへ高速結合する"""
        if not wav_paths:
            raise ValueError("結合する音声セグメントが存在しません")

        master_out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(wav_paths[0]), "rb") as first_rf:
            params = first_rf.getparams()

        with wave.open(str(master_out_path), "wb") as out_wf:
            out_wf.setparams(params)
            for p in wav_paths:
                with wave.open(str(p), "rb") as rf:
                    out_wf.writeframes(rf.readframes(rf.getnframes()))
