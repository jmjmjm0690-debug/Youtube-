import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

from src.config_loader import AppConfig
from src.script_generator import GeneratedEpisode
from src.visual_renderer import VisualRenderer
from src.voicevox_client import AudioTimelineResult, SynthesizedSegment

logger = logging.getLogger(__name__)


class VideoComposer:
    """
    Pillowで描画した各セリフの口パク開閉フレームペアと、VOICEVOXマスターナレーション音声、
    および著作権フリーBGMを FFmpeg concat デマルチプレクサで高速合成し、
    1920x1080 / 30fps の MP4 動画を出力するモジュール。
    """

    def __init__(self, config: AppConfig, renderer: VisualRenderer) -> None:
        self.config = config
        self.renderer = renderer
        self.video_cfg = config.settings.get("video", {})
        self.fps = int(self.video_cfg.get("fps", 30))
        self.video_codec = str(self.video_cfg.get("video_codec", "libx264"))
        self.video_preset = str(self.video_cfg.get("video_preset", "veryfast"))
        self.video_crf = int(self.video_cfg.get("video_crf", 20))
        self.audio_bitrate = str(self.video_cfg.get("audio_bitrate", "192k"))
        self.voice_boost = float(self.video_cfg.get("voice_boost_gain", 2.2))
        self.bgm_volume = float(self.video_cfg.get("bgm_volume_ratio", 0.14))
        self.flap_interval = float(
            self.video_cfg.get("mouth_flap_interval_sec", 0.18)
        )

    def _find_bgm_file(self) -> Path:
        bgm_dir = self.config.root_dir / "assets" / "bgm"
        for ext in ("*.wav", "*.mp3", "*.m4a", "*.ogg"):
            files = sorted(bgm_dir.glob(ext))
            if files:
                return files[0]
        raise FileNotFoundError(f"BGMファイルが見つかりません: {bgm_dir}")

    def _build_concat_schedule(
        self,
        segments: List[SynthesizedSegment],
        frame_pairs: List[Tuple[Path, Path]],
        concat_txt_path: Path,
    ) -> None:
        """
        各セリフの発話区間 (speech_duration_sec) では口開き(open)と口閉じ(close)を交互に切り替え、
        セリフ間ポーズ区間 (pause_duration_sec) では口閉じ(close)を表示する FFmpeg concat 定義を生成。
        """
        lines: List[str] = ["ffconcat version 1.0"]
        last_close_path: Path | None = None

        for seg, (close_path, open_path) in zip(segments, frame_pairs):
            close_posix = close_path.resolve().as_posix()
            open_posix = open_path.resolve().as_posix()
            last_close_path = close_path

            remaining_speech = max(0.04, seg.speech_duration_sec)
            is_open = True

            while remaining_speech > 0.005:
                step = min(self.flap_interval, remaining_speech)
                target_img = open_posix if is_open else close_posix
                lines.append(f"file '{target_img}'")
                lines.append(f"duration {step:.4f}")
                remaining_speech -= step
                is_open = not is_open

            if seg.pause_duration_sec > 0.005:
                lines.append(f"file '{close_posix}'")
                lines.append(f"duration {seg.pause_duration_sec:.4f}")

        # FFmpeg concat demuxer 仕様上、最後のフレームを末尾にもう一度記載する
        if last_close_path is not None:
            lines.append(f"file '{last_close_path.resolve().as_posix()}'")

        concat_txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _resolve_ffmpeg_executable() -> str:
        """システムPATHの ffmpeg を優先し、未導入環境(Windows等)では imageio-ffmpeg 同梱バイナリを使用する"""
        sys_ffmpeg = shutil.which("ffmpeg")
        if sys_ffmpeg:
            return sys_ffmpeg
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            raise RuntimeError(
                "システムに ffmpeg が見つかりません。ffmpeg または imageio-ffmpeg をインストールしてください。"
            )

    def compose_video(
        self,
        episode: GeneratedEpisode,
        timeline: AudioTimelineResult,
        work_dir: Path,
        output_mp4_path: Path,
    ) -> Path:
        """
        全セグメントのフレーム描画 → FFmpeg 口パクタイムライン構築 → BGMミキシング＆MP4エンコードを実行する。
        """
        ffmpeg_exe = self._resolve_ffmpeg_executable()

        frames_dir = work_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        total_segs = len(timeline.segments)
        logger.info("全 %d セリフ分の解説スライド・立ち絵・字幕フレームを描画中...", total_segs)

        metan_emotion = "normal"
        zunda_emotion = "normal"
        frame_pairs: List[Tuple[Path, Path]] = []

        for seg in timeline.segments:
            if seg.speaker == "metan":
                metan_emotion = seg.emotion
            else:
                zunda_emotion = seg.emotion

            close_png, open_png = self.renderer.render_segment_frame_pair(
                episode=episode,
                segment=seg,
                total_segments=total_segs,
                metan_emotion=metan_emotion,
                zunda_emotion=zunda_emotion,
                frames_dir=frames_dir,
            )
            frame_pairs.append((close_png, open_png))

        concat_txt_path = work_dir / "frames_concat.txt"
        self._build_concat_schedule(
            timeline.segments, frame_pairs, concat_txt_path
        )

        bgm_path = self._find_bgm_file()
        output_mp4_path.parent.mkdir(parents=True, exist_ok=True)

        total_dur_str = f"{timeline.total_duration_sec:.2f}"

        # FFmpeg コマンド組み立て:
        # 入力0: concat画像列 / 入力1: マスターナレーションWAV / 入力2: 無限ループBGM
        cmd = [
            ffmpeg_exe,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_txt_path.resolve()),
            "-i",
            str(timeline.master_voice_wav_path.resolve()),
            "-stream_loop",
            "-1",
            "-i",
            str(bgm_path.resolve()),
            "-filter_complex",
            (
                f"[1:a]volume={self.voice_boost:.2f},dynaudnorm=f=150:g=11:p=0.95[voice];"
                f"[2:a]volume={self.bgm_volume:.3f}[bgm];"
                f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
                f"alimiter=limit=0.98:level=false[aout]"
            ),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            self.video_codec,
            "-preset",
            self.video_preset,
            "-tune",
            "stillimage",
            "-crf",
            str(self.video_crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self.fps),
            "-c:a",
            "aac",
            "-b:a",
            self.audio_bitrate,
            "-t",
            total_dur_str,
            "-movflags",
            "+faststart",
            str(output_mp4_path.resolve()),
        ]

        logger.info("FFmpeg による 1080p MP4 エンコードを開始します (総尺: %s秒)...", total_dur_str)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            logger.error("FFmpeg エンコード失敗:\n%s", proc.stderr[-1500:])
            raise RuntimeError(f"FFmpeg エンコードエラー (code={proc.returncode})")

        size_mb = output_mp4_path.stat().st_size / (1024 * 1024)
        logger.info(
            "1080p MP4 動画出力完了: %s (%.2f MB)", output_mp4_path, size_mb
        )
        return output_mp4_path
