import logging
import math
import re
import textwrap
from pathlib import Path
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

from scripts.generate_default_assets import ensure_default_assets
from src.config_loader import AppConfig
from src.script_generator import GeneratedEpisode, SlideCard
from src.voicevox_client import SynthesizedSegment

logger = logging.getLogger(__name__)


def _hex_to_rgba(hex_str: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) == 6:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
    return (30, 40, 70, alpha)


class VisualRenderer:
    """
    1920x1080 (16:9) の解説動画フレームおよび 1280x720 の高CTRサムネイル画像を描画するレンダラー。
    - キャラクター立ち絵は非発話時も暗くせず、常に100%の明るさ・鮮やかな状態で表示
    - 話している考察・話題に合わせて、後ろの大型モニター画面と背景テーマが
      （注目度メーター / 時系列フロー / バーチャート / 世論円グラフ / 構造相関図 / 左右VS比較 / 未来予測グラフ / 対策ボード）
      へ動的に切り替わるビジュアル演出
    """

    # 図解タイプごとのスタジオ背景カラーパレット (上段色, 下段色, アクセント色, モニター枠内背景色)
    SCENE_PALETTES: Dict[str, Tuple[str, str, str, str]] = {
        "trend_meter": ("#1F0D29", "#3B123C", "#FF4D6D", "#1A1228"),
        "timeline": ("#0A1628", "#162B4D", "#00E5FF", "#111E36"),
        "bar_chart": ("#0B1F24", "#153B44", "#00F5D4", "#102730"),
        "public_reaction": ("#161233", "#282059", "#A78BFA", "#1B163C"),
        "structure_map": ("#1D1128", "#321B47", "#F59E0B", "#211530"),
        "comparison": ("#0F172A", "#1E293B", "#38BDF8", "#141E33"),
        "future_forecast": ("#0C192C", "#1B365D", "#FBBF24", "#12233D"),
        "action_summary": ("#122229", "#1E3A46", "#4ADE80", "#152932"),
        "bullet_points": ("#0D1322", "#1B263B", "#4EA8DE", "#162035"),
        "key_metric": ("#1A1423", "#2D233D", "#F59E0B", "#231B30"),
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.video_cfg = config.settings.get("video", {})
        self.vv_cfg = config.settings.get("voicevox", {})
        self.width = int(self.video_cfg.get("width", 1920))
        self.height = int(self.video_cfg.get("height", 1080))
        self.thumb_w = int(
            config.settings.get("thumbnail", {}).get("width", 1280)
        )
        self.thumb_h = int(
            config.settings.get("thumbnail", {}).get("height", 720)
        )

        ensure_default_assets(config.root_dir)

        self._font_cache: Dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._bg_cache: Dict[str, Image.Image] = {}
        self._sprite_cache: Dict[Tuple[str, str, str], Image.Image] = {}

    def get_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if size in self._font_cache:
            return self._font_cache[size]

        custom_fonts_dir = self.config.root_dir / "assets" / "fonts"
        custom_candidates = (
            list(custom_fonts_dir.glob("*.ttc"))
            + list(custom_fonts_dir.glob("*.ttf"))
            + list(custom_fonts_dir.glob("*.otf"))
            if custom_fonts_dir.exists()
            else []
        )

        system_candidates = [
            *[str(p) for p in custom_candidates],
            "C:/Windows/Fonts/meiryob.ttc",
            "C:/Windows/Fonts/YuGothB.ttc",
            "C:/Windows/Fonts/msgothic.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        for fpath in system_candidates:
            if Path(fpath).exists():
                try:
                    font = ImageFont.truetype(fpath, size)
                    self._font_cache[size] = font
                    return font
                except Exception:
                    continue

        default_font = ImageFont.load_default()
        self._font_cache[size] = default_font
        return default_font

    def _get_scene_background(self, diagram_type: str) -> Image.Image:
        """話題の図解タイプに合わせてスタジオ背景の色彩・照明エフェクトを切り替える"""
        palette_key = (
            diagram_type if diagram_type in self.SCENE_PALETTES else "bullet_points"
        )
        if palette_key in self._bg_cache:
            return self._bg_cache[palette_key].copy()

        top_hex, bot_hex, accent_hex, _ = self.SCENE_PALETTES[palette_key]
        top_rgb = _hex_to_rgba(top_hex)
        bot_rgb = _hex_to_rgba(bot_hex)
        accent_glow = _hex_to_rgba(accent_hex, 38)

        bg = Image.new("RGBA", (self.width, self.height), top_rgb)
        draw = ImageDraw.Draw(bg)

        for y in range(self.height):
            ratio = y / float(self.height)
            r = int(top_rgb[0] * (1 - ratio) + bot_rgb[0] * ratio)
            g = int(top_rgb[1] * (1 - ratio) + bot_rgb[1] * ratio)
            b = int(top_rgb[2] * (1 - ratio) + bot_rgb[2] * ratio)
            draw.line([(0, y), (self.width, y)], fill=(r, g, b, 255))

        # 幾何学グリッドラインと装飾サークル
        grid_color = (35, 50, 82, 255)
        for x in range(0, self.width, 80):
            draw.line([(x, 0), (x, self.height)], fill=grid_color, width=1)
        for y in range(0, self.height, 80):
            draw.line([(0, y), (self.width, y)], fill=grid_color, width=1)

        draw.ellipse((-160, -140, 460, 460), outline=_hex_to_rgba(accent_hex, 255), width=3)
        draw.ellipse(
            (self.width - 480, self.height - 480, self.width + 150, self.height + 150),
            outline=_hex_to_rgba(accent_hex, 255),
            width=3,
        )

        self._bg_cache[palette_key] = bg
        return bg.copy()

    def _load_character_sprite(
        self,
        char_id: str,
        emotion: str,
        mouth_state: str,
    ) -> Image.Image:
        """
        キャラクター立ち絵を読み込む。
        非発話時も暗くせず、常に100%の鮮やかな明度・同一サイズで表示する。
        """
        cache_key = (char_id, emotion, mouth_state)
        if cache_key in self._sprite_cache:
            return self._sprite_cache[cache_key]

        char_dir = self.config.root_dir / "assets" / "characters" / char_id
        candidates = [
            char_dir / f"{emotion}_{mouth_state}.png",
            char_dir / f"{emotion}.png",
            char_dir / f"normal_{mouth_state}.png",
            char_dir / "normal_close.png",
            char_dir / "normal.png",
        ]
        sprite_path = next((p for p in candidates if p.exists()), candidates[0])
        img = Image.open(sprite_path).convert("RGBA")

        target_h = 645
        aspect = img.width / max(1, img.height)
        target_w = int(target_h * aspect)
        img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        self._sprite_cache[cache_key] = img
        return img

    def _draw_outlined_text(
        self,
        draw: ImageDraw.ImageDraw,
        xy: Tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        fill: Tuple[int, int, int, int],
        outline_color: Tuple[int, int, int, int],
        outline_width: int = 4,
    ) -> None:
        draw.text(
            xy,
            text,
            font=font,
            fill=fill,
            stroke_width=outline_width,
            stroke_fill=outline_color,
        )

    @staticmethod
    def _extract_percentage(text: str, default_val: int) -> int:
        m = re.search(r"(\d{1,3})\s*[%％]", text)
        if m:
            return max(10, min(100, int(m.group(1))))
        return default_val

    def _render_trend_meter_visual(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        points: List[str],
        accent_rgba: Tuple[int, int, int, int],
    ) -> None:
        """1. 急上昇トレンド注目度スピードメーター＆急上昇チャート画面"""
        x0, y0, x1, y1 = box
        # 左側: 大型スピードメーター＋急上昇グラフパネル
        lx0, ly0, lx1, ly1 = x0, y0, x0 + 390, y1
        draw.rounded_rectangle(
            (lx0, ly0, lx1, ly1),
            radius=18,
            fill=(16, 22, 42, 255),
            outline=accent_rgba,
            width=2,
        )
        mcx, mcy, mr = (lx0 + lx1) // 2, ly0 + 165, 115
        # メーター背景アーク
        draw.arc(
            (mcx - mr, mcy - mr, mcx + mr, mcy + mr),
            start=150,
            end=390,
            fill=(55, 65, 95, 255),
            width=22,
        )
        # メーターアクティブアーク (96% HOT)
        draw.arc(
            (mcx - mr, mcy - mr, mcx + mr, mcy + mr),
            start=150,
            end=378,
            fill=(255, 77, 109, 255),
            width=22,
        )
        draw.text(
            (mcx, mcy - 16),
            "注目度 96%",
            font=self.get_font(32),
            anchor="mm",
            fill=(255, 230, 60, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        draw.text(
            (mcx, mcy + 26),
            "TREND SURGE",
            font=self.get_font(19),
            anchor="mm",
            fill=(255, 160, 180, 255),
        )

        # 下部に急上昇折れ線スパークライン
        gx0, gy0, gx1, gy1 = lx0 + 32, ly0 + 270, lx1 - 32, ly1 - 24
        draw.line([(gx0, gy1), (gx1, gy1)], fill=(120, 140, 180, 255), width=2)
        draw.line([(gx0, gy0), (gx0, gy1)], fill=(120, 140, 180, 255), width=2)
        spark_pts = [
            (gx0 + 10, gy1 - 16),
            (gx0 + 75, gy1 - 24),
            (gx0 + 140, gy1 - 34),
            (gx0 + 205, gy1 - 72),
            (gx0 + 265, gy0 + 16),
            (gx1 - 10, gy0 + 4),
        ]
        draw.line(spark_pts, fill=(0, 245, 212, 255), width=6)
        for px, py in spark_pts:
            draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=(255, 230, 60, 255))

        # 右側: 核心ポイントカード
        rx0 = lx1 + 18
        card_h = (y1 - y0 - 24) // max(1, len(points[:3]))
        for idx, pt in enumerate(points[:3]):
            cy0 = y0 + idx * (card_h + 12)
            cy1 = cy0 + card_h
            draw.rounded_rectangle(
                (rx0, cy0, x1, cy1),
                radius=14,
                fill=(22, 30, 54, 255),
                outline=accent_rgba if idx == 0 else (75, 95, 140, 255),
                width=3 if idx == 0 else 2,
            )
            draw.rounded_rectangle(
                (rx0 + 14, cy0 + 18, rx0 + 114, cy1 - 18),
                radius=10,
                fill=accent_rgba,
            )
            draw.text(
                (rx0 + 64, (cy0 + cy1) // 2),
                f"POINT {idx + 1}",
                font=self.get_font(19),
                anchor="mm",
                fill=(15, 15, 25, 255),
            )
            wrapped = textwrap.wrap(pt, width=21)[:2]
            for l_i, line in enumerate(wrapped):
                self._draw_outlined_text(
                    draw,
                    (rx0 + 130, cy0 + 22 + l_i * 36),
                    line,
                    self.get_font(25),
                    fill=(255, 255, 255, 255),
                    outline_color=(0, 0, 0, 230),
                    outline_width=2,
                )

    def _render_bar_chart_visual(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        points: List[str],
        accent_rgba: Tuple[int, int, int, int],
    ) -> None:
        """2. データ・要因・ボトルネック比較バーチャート画面"""
        x0, y0, x1, y1 = box
        defaults = [88, 76, 92, 81]
        bar_colors = [
            (0, 245, 212, 255),
            (56, 189, 248, 255),
            (251, 191, 36, 255),
            (244, 114, 182, 255),
        ]
        items = points[:3]
        row_h = (y1 - y0 - 20) // max(1, len(items))

        for idx, pt in enumerate(items):
            pct = self._extract_percentage(pt, defaults[idx % len(defaults)])
            clean_label = re.sub(r"\(\d{1,3}\s*[%％]\)", "", pt).strip()
            ry0 = y0 + idx * (row_h + 10)
            ry1 = ry0 + row_h
            b_color = bar_colors[idx % len(bar_colors)]

            draw.rounded_rectangle(
                (x0, ry0, x1, ry1),
                radius=14,
                fill=(18, 32, 48, 255),
                outline=(65, 95, 130, 255),
                width=2,
            )
            self._draw_outlined_text(
                draw,
                (x0 + 24, ry0 + 14),
                clean_label[:33],
                self.get_font(26),
                fill=(255, 255, 255, 255),
                outline_color=(0, 0, 0, 230),
                outline_width=2,
            )
            bx0, by0, bx1, by1 = x0 + 24, ry0 + 60, x1 - 135, ry1 - 18
            draw.rounded_rectangle(
                (bx0, by0, bx1, by1),
                radius=12,
                fill=(10, 16, 28, 255),
                outline=(75, 100, 140, 255),
                width=1,
            )
            fill_w = int((bx1 - bx0) * (pct / 100.0))
            draw.rounded_rectangle(
                (bx0, by0, bx0 + max(30, fill_w), by1),
                radius=12,
                fill=b_color,
            )
            draw.text(
                (x1 - 70, (by0 + by1) // 2),
                f"{pct}%",
                font=self.get_font(31),
                anchor="mm",
                fill=b_color,
                stroke_width=3,
                stroke_fill=(0, 0, 0, 230),
            )

    def _render_public_reaction_visual(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        points: List[str],
    ) -> None:
        """3. 世間・SNSの賛否リアクション円グラフ（ドーナツチャート）＋吹き出し画面"""
        x0, y0, x1, y1 = box
        lx0, ly0, lx1, ly1 = x0, y0, x0 + 380, y1
        draw.rounded_rectangle(
            (lx0, ly0, lx1, ly1),
            radius=18,
            fill=(20, 18, 48, 255),
            outline=(167, 139, 250, 255),
            width=2,
        )
        cx, cy, r = (lx0 + lx1) // 2, (ly0 + ly1) // 2 - 18, 122
        slices = [
            (0, 173, (56, 189, 248, 255), "推進48%"),
            (173, 295, (244, 63, 94, 255), "慎重34%"),
            (295, 360, (250, 204, 21, 255), "静観18%"),
        ]
        for s_deg, e_deg, col, _ in slices:
            draw.pieslice(
                (cx - r, cy - r, cx + r, cy + r),
                start=s_deg - 90,
                end=e_deg - 90,
                fill=col,
                outline=(18, 20, 38, 255),
                width=3,
            )
        ir = 64
        draw.ellipse(
            (cx - ir, cy - ir, cx + ir, cy + ir),
            fill=(20, 18, 48, 255),
            outline=(120, 110, 180, 255),
            width=2,
        )
        draw.text(
            (cx, cy),
            "SNS世論",
            font=self.get_font(23),
            anchor="mm",
            fill=(255, 255, 255, 255),
        )
        for idx, (_, _, col, label) in enumerate(slices):
            lx = lx0 + 18 + idx * 116
            draw.rectangle((lx, ly1 - 40, lx + 16, ly1 - 24), fill=col)
            draw.text(
                (lx + 22, ly1 - 43),
                label,
                font=self.get_font(17),
                fill=(235, 240, 255, 255),
            )

        rx0 = lx1 + 18
        colors = [
            (56, 189, 248, 255),
            (244, 63, 94, 255),
            (250, 204, 21, 255),
        ]
        tags = ["推進・期待", "慎重・懸念", "中立・静観"]
        card_h = (y1 - y0 - 24) // max(1, len(points[:3]))
        for idx, pt in enumerate(points[:3]):
            cy0 = y0 + idx * (card_h + 12)
            cy1 = cy0 + card_h
            col = colors[idx % len(colors)]
            draw.rounded_rectangle(
                (rx0, cy0, x1, cy1),
                radius=16,
                fill=(24, 22, 56, 255),
                outline=col,
                width=2,
            )
            draw.rounded_rectangle(
                (rx0 + 14, cy0 + 18, rx0 + 142, cy1 - 18),
                radius=10,
                fill=col,
            )
            draw.text(
                (rx0 + 78, (cy0 + cy1) // 2),
                tags[idx % len(tags)],
                font=self.get_font(20),
                anchor="mm",
                fill=(15, 15, 25, 255),
            )
            wrapped = textwrap.wrap(pt, width=20)[:2]
            for l_i, line in enumerate(wrapped):
                self._draw_outlined_text(
                    draw,
                    (rx0 + 158, cy0 + 22 + l_i * 36),
                    line,
                    self.get_font(24),
                    fill=(255, 255, 255, 255),
                    outline_color=(0, 0, 0, 230),
                    outline_width=2,
                )

    def _render_structure_map_visual(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        points: List[str],
        topic_keyword: str,
        accent_rgba: Tuple[int, int, int, int],
    ) -> None:
        """4. なぜ起きた？3大要因の構造相関マップ"""
        x0, y0, x1, y1 = box
        core_x0, core_y0, core_x1, core_y1 = (
            x0 + 8,
            (y0 + y1) // 2 - 90,
            x0 + 295,
            (y0 + y1) // 2 + 90,
        )
        rx0 = x0 + 365
        items = points[:3]
        card_h = (y1 - y0 - 24) // max(1, len(items))
        node_colors = [
            (0, 245, 212, 255),
            (251, 191, 36, 255),
            (244, 114, 182, 255),
        ]

        core_right_x = core_x1
        core_center_y = (core_y0 + core_y1) // 2
        for idx in range(len(items)):
            cy0 = y0 + idx * (card_h + 12)
            target_y = cy0 + card_h // 2
            col = node_colors[idx % len(node_colors)]
            draw.line(
                [(core_right_x, core_center_y), (rx0, target_y)],
                fill=col,
                width=5,
            )
            draw.polygon(
                [
                    (rx0, target_y),
                    (rx0 - 16, target_y - 10),
                    (rx0 - 16, target_y + 10),
                ],
                fill=col,
            )

        draw.rounded_rectangle(
            (core_x0, core_y0, core_x1, core_y1),
            radius=20,
            fill=(45, 24, 70, 255),
            outline=accent_rgba,
            width=4,
        )
        draw.text(
            ((core_x0 + core_x1) // 2, core_y0 + 36),
            "構造変化の核心",
            font=self.get_font(21),
            anchor="mm",
            fill=accent_rgba,
        )
        kw_lines = textwrap.wrap(topic_keyword[:18], width=9)[:2]
        for i, kl in enumerate(kw_lines):
            self._draw_outlined_text(
                draw,
                (core_x0 + 24, core_y0 + 68 + i * 38),
                kl,
                self.get_font(26),
                fill=(255, 255, 255, 255),
                outline_color=(0, 0, 0, 230),
                outline_width=2,
            )

        for idx, pt in enumerate(items):
            cy0 = y0 + idx * (card_h + 12)
            cy1 = cy0 + card_h
            col = node_colors[idx % len(node_colors)]
            draw.rounded_rectangle(
                (rx0, cy0, x1, cy1),
                radius=16,
                fill=(26, 20, 48, 255),
                outline=col,
                width=3,
            )
            draw.ellipse(
                (rx0 + 16, (cy0 + cy1) // 2 - 26, rx0 + 68, (cy0 + cy1) // 2 + 26),
                fill=col,
            )
            draw.text(
                (rx0 + 42, (cy0 + cy1) // 2),
                f"要因{idx + 1}",
                font=self.get_font(19),
                anchor="mm",
                fill=(10, 15, 25, 255),
            )
            wrapped = textwrap.wrap(pt, width=23)[:2]
            for l_i, line in enumerate(wrapped):
                self._draw_outlined_text(
                    draw,
                    (rx0 + 84, cy0 + 22 + l_i * 36),
                    line,
                    self.get_font(25),
                    fill=(255, 255, 255, 255),
                    outline_color=(0, 0, 0, 230),
                    outline_width=2,
                )

    def _render_comparison_visual(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        points: List[str],
    ) -> None:
        """5. 左右2カラム VS 徹底比較ボード（従来 vs 新時代 / メリット vs デメリット・注意点）"""
        x0, y0, x1, y1 = box
        mid_x = (x0 + x1) // 2
        col_w = (x1 - x0 - 44) // 2

        left_box = (x0, y0, x0 + col_w, y1)
        right_box = (x1 - col_w, y0, x1, y1)

        # 左カラム（ブルー/グリーン系: 従来構造 または メリット・推進視点）
        draw.rounded_rectangle(
            left_box,
            radius=18,
            fill=(16, 48, 68, 215),
            outline=(56, 189, 248, 255),
            width=3,
        )
        draw.rounded_rectangle(
            (left_box[0] + 14, left_box[1] + 14, left_box[2] - 14, left_box[1] + 72),
            radius=12,
            fill=(56, 189, 248, 255),
        )
        draw.text(
            ((left_box[0] + left_box[2]) // 2, left_box[1] + 43),
            "視点A：メリット・推進要因",
            font=self.get_font(25),
            anchor="mm",
            fill=(10, 18, 30, 255),
        )

        # 右カラム（レッド/アンバー系: 変革・課題 または デメリット・注意点）
        draw.rounded_rectangle(
            right_box,
            radius=18,
            fill=(68, 22, 45, 215),
            outline=(251, 113, 133, 255),
            width=3,
        )
        draw.rounded_rectangle(
            (right_box[0] + 14, right_box[1] + 14, right_box[2] - 14, right_box[1] + 72),
            radius=12,
            fill=(251, 113, 133, 255),
        )
        draw.text(
            ((right_box[0] + right_box[2]) // 2, right_box[1] + 43),
            "視点B：転換点・注意点と課題",
            font=self.get_font(25),
            anchor="mm",
            fill=(25, 10, 15, 255),
        )

        # 左右に項目を振り分け
        left_items = points[:2] if len(points) >= 4 else points[::2]
        right_items = points[2:4] if len(points) >= 4 else points[1::2]
        if not right_items:
            right_items = ["構造変化に伴う移行コストと新ルールへの適応"]

        for side_box, side_items, accent_col in (
            (left_box, left_items, (125, 211, 252, 255)),
            (right_box, right_items, (253, 164, 175, 255)),
        ):
            for idx, pt in enumerate(side_items[:2]):
                iy0 = side_box[1] + 92 + idx * 145
                iy1 = iy0 + 130
                draw.rounded_rectangle(
                    (side_box[0] + 16, iy0, side_box[2] - 16, iy1),
                    radius=12,
                    fill=(0, 0, 0, 85),
                    outline=accent_col,
                    width=1,
                )
                wrapped = textwrap.wrap(pt, width=18)[:3]
                for l_i, line in enumerate(wrapped):
                    self._draw_outlined_text(
                        draw,
                        (side_box[0] + 30, iy0 + 16 + l_i * 34),
                        line,
                        self.get_font(25),
                        fill=(255, 255, 255, 255),
                        outline_color=(0, 0, 0, 210),
                        outline_width=2,
                    )

        # 中央 VS エンブレム
        cy_mid = (y0 + y1) // 2
        draw.ellipse(
            (mid_x - 34, cy_mid - 34, mid_x + 34, cy_mid + 34),
            fill=(245, 158, 11, 255),
            outline=(255, 255, 255, 255),
            width=3,
        )
        draw.text(
            (mid_x, cy_mid),
            "VS",
            font=self.get_font(26),
            anchor="mm",
            fill=(15, 15, 25, 255),
        )

    def _render_future_forecast_visual(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        points: List[str],
    ) -> None:
        """6. 今後1〜3年の未来シナリオ予測グラフ曲線＋シナリオ解説画面"""
        x0, y0, x1, y1 = box
        # 左側: 2本の将来シナリオ成長曲線グラフ
        lx0, ly0, lx1, ly1 = x0, y0, x0 + 460, y1
        draw.rounded_rectangle(
            (lx0, ly0, lx1, ly1),
            radius=18,
            fill=(12, 22, 42, 230),
            outline=(251, 191, 36, 200),
            width=2,
        )
        draw.text(
            ((lx0 + lx1) // 2, ly0 + 28),
            "今後1〜3年の展開シナリオ予測",
            font=self.get_font(23),
            anchor="mm",
            fill=(251, 191, 36, 255),
        )
        gx0, gy0, gx1, gy1 = lx0 + 45, ly0 + 65, lx1 - 35, ly1 - 50
        # グリッドと軸
        for gy in range(gy0, gy1 + 1, (gy1 - gy0) // 3):
            draw.line([(gx0, gy), (gx1, gy)], fill=(255, 255, 255, 30), width=1)
        draw.line([(gx0, gy1), (gx1, gy1)], fill=(255, 255, 255, 160), width=3)
        draw.line([(gx0, gy0), (gx0, gy1)], fill=(255, 255, 255, 160), width=3)

        # シナリオA（順調加速ルート: グリーン曲線）
        curve_a = [
            (gx0, gy1 - 30),
            (gx0 + 95, gy1 - 80),
            (gx0 + 190, gy1 - 155),
            (gx0 + 285, gy0 + 45),
            (gx1, gy0 + 10),
        ]
        draw.line(curve_a, fill=(74, 222, 128, 255), width=6)
        draw.text(
            (gx1 - 65, gy0 + 28),
            "シナリオA",
            font=self.get_font(20),
            fill=(74, 222, 128, 255),
        )

        # シナリオB（調整・再編ルート: オレンジ曲線）
        curve_b = [
            (gx0, gy1 - 30),
            (gx0 + 95, gy1 - 60),
            (gx0 + 190, gy1 - 45),
            (gx0 + 285, gy1 - 110),
            (gx1, gy0 + 95),
        ]
        draw.line(curve_b, fill=(251, 191, 36, 255), width=5)
        draw.text(
            (gx1 - 65, gy0 + 115),
            "シナリオB",
            font=self.get_font(20),
            fill=(251, 191, 36, 255),
        )

        for idx, t_label in enumerate(["現在", "1年後", "2年後", "3年後"]):
            tx = gx0 + idx * ((gx1 - gx0) // 3)
            draw.text(
                (tx, gy1 + 22),
                t_label,
                font=self.get_font(19),
                anchor="mm",
                fill=(210, 225, 245, 255),
            )

        # 右側: シナリオ解説カード
        rx0 = lx1 + 18
        cols = [
            (74, 222, 128, 255),
            (251, 191, 36, 255),
            (56, 189, 248, 255),
        ]
        card_h = (y1 - y0 - 24) // max(1, len(points[:3]))
        for idx, pt in enumerate(points[:3]):
            cy0 = y0 + idx * (card_h + 12)
            cy1 = cy0 + card_h
            col = cols[idx % len(cols)]
            draw.rounded_rectangle(
                (rx0, cy0, x1, cy1),
                radius=15,
                fill=(16, 28, 52, 255),
                outline=col,
                width=3,
            )
            draw.rounded_rectangle(
                (rx0, cy0, rx0 + 14, cy1),
                radius=6,
                fill=col,
            )
            wrapped = textwrap.wrap(pt, width=22)[:2]
            for l_i, line in enumerate(wrapped):
                self._draw_outlined_text(
                    draw,
                    (rx0 + 28, cy0 + 22 + l_i * 36),
                    line,
                    self.get_font(25),
                    fill=(255, 255, 255, 255),
                    outline_color=(0, 0, 0, 230),
                    outline_width=2,
                )

    def _draw_slide_card(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        slide: SlideCard,
        section_title: str,
        section_index: int,
        total_sections: int,
        topic_keyword: str,
        category: str,
        progress_ratio: float,
    ) -> None:
        """
        画面上部チャプターバーおよび、四国めたん＆ずんだもんの後ろに映る
        大型ビジュアル解説モニター画面（グラフ・相関図・比較ボード・時系列フロー）を描画する。
        """
        dtype = slide.diagram_type
        _, _, accent_hex, card_bg_hex = self.SCENE_PALETTES.get(
            dtype, self.SCENE_PALETTES["bullet_points"]
        )
        accent_rgba = _hex_to_rgba(accent_hex)
        card_bg_rgba = _hex_to_rgba(card_bg_hex, 255)

        # 1. 上部チャプターヘッダーバー
        draw.rectangle((0, 0, self.width, 74), fill=(10, 15, 28, 255))
        draw.rectangle(
            (0, 70, int(self.width * max(0.02, min(1.0, progress_ratio))), 74),
            fill=accent_rgba,
        )

        badge_label = f"CHAPTER {section_index + 1}/{total_sections}"
        draw.rounded_rectangle((26, 13, 232, 59), radius=10, fill=accent_rgba)
        draw.text(
            (42, 21),
            badge_label,
            font=self.get_font(22),
            fill=(10, 15, 30, 255),
        )

        self._draw_outlined_text(
            draw,
            (252, 17),
            section_title[:38],
            self.get_font(29),
            fill=(255, 255, 255, 255),
            outline_color=(0, 0, 0, 230),
            outline_width=3,
        )

        # 2. 後ろの大型ビジュアルモニター画面 (左右の立ち絵と被らない中央領域 x=348..1572, y=92..742)
        cx0, cy0, cx1, cy1 = 348, 92, 1572, 742
        draw.rounded_rectangle(
            (cx0, cy0, cx1, cy1),
            radius=24,
            fill=card_bg_rgba,
            outline=accent_rgba,
            width=4,
        )

        # モニター画面の上部タイトル帯
        draw.rounded_rectangle(
            (cx0 + 18, cy0 + 16, cx1 - 18, cy0 + 92),
            radius=14,
            fill=(10, 16, 30, 255),
            outline=(60, 80, 120, 255),
            width=2,
        )

        if slide.highlight_keyword:
            draw.rounded_rectangle(
                (cx0 + 30, cy0 + 28, cx0 + 245, cy0 + 80),
                radius=10,
                fill=accent_rgba,
            )
            draw.text(
                (cx0 + 137, cy0 + 54),
                slide.highlight_keyword[:8],
                font=self.get_font(23),
                anchor="mm",
                fill=(10, 15, 28, 255),
            )
            title_x = cx0 + 265
        else:
            title_x = cx0 + 34

        self._draw_outlined_text(
            draw,
            (title_x, cy0 + 32),
            slide.slide_title[:24],
            self.get_font(33),
            fill=(255, 255, 255, 255),
            outline_color=(0, 0, 0, 230),
            outline_width=3,
        )

        # モニター内の図解コンテンツ描画領域
        inner_box = (cx0 + 26, cy0 + 108, cx1 - 26, cy1 - 22)
        points = slide.slide_points or ["トレンドの背景と今後の影響を多角的に分析"]

        if dtype == "trend_meter":
            self._render_trend_meter_visual(draw, inner_box, points, accent_rgba)
        elif dtype == "bar_chart":
            self._render_bar_chart_visual(draw, inner_box, points, accent_rgba)
        elif dtype == "public_reaction":
            self._render_public_reaction_visual(draw, inner_box, points)
        elif dtype == "structure_map":
            self._render_structure_map_visual(
                draw, inner_box, points, topic_keyword, accent_rgba
            )
        elif dtype == "comparison":
            self._render_comparison_visual(draw, inner_box, points)
        elif dtype == "future_forecast":
            self._render_future_forecast_visual(draw, inner_box, points)
        elif dtype == "timeline":
            ix0, iy0, ix1, iy1 = inner_box
            step_h = (iy1 - iy0 - 20) // max(1, len(points[:3]))
            for idx, pt in enumerate(points[:3]):
                py0 = iy0 + idx * (step_h + 12)
                py1 = py0 + step_h
                cx_circle = ix0 + 48
                cy_circle = (py0 + py1) // 2

                if idx < len(points[:3]) - 1:
                    draw.line(
                        [(cx_circle, cy_circle + 28), (cx_circle, py1 + 24)],
                        fill=accent_rgba,
                        width=6,
                    )

                draw.ellipse(
                    (cx_circle - 30, cy_circle - 30, cx_circle + 30, cy_circle + 30),
                    fill=accent_rgba,
                )
                draw.text(
                    (cx_circle, cy_circle),
                    f"0{idx + 1}",
                    font=self.get_font(23),
                    anchor="mm",
                    fill=(10, 15, 30, 255),
                )
                draw.rounded_rectangle(
                    (ix0 + 96, py0 + 4, ix1, py1 - 4),
                    radius=14,
                    fill=(18, 30, 54, 255),
                    outline=accent_rgba if idx == 1 else (70, 95, 140, 255),
                    width=3 if idx == 1 else 2,
                )
                self._draw_outlined_text(
                    draw,
                    (ix0 + 118, cy_circle - 18),
                    pt[:32],
                    self.get_font(27),
                    fill=(255, 255, 255, 255),
                    outline_color=(0, 0, 0, 230),
                    outline_width=2,
                )
        else:
            ix0, iy0, ix1, iy1 = inner_box
            item_h = (iy1 - iy0 - 24) // max(1, len(points[:3]))
            for idx, pt in enumerate(points[:3]):
                py0 = iy0 + idx * (item_h + 12)
                py1 = py0 + item_h

                draw.rounded_rectangle(
                    (ix0, py0, ix1, py1),
                    radius=16,
                    fill=(18, 32, 46, 255),
                    outline=accent_rgba if idx == 0 else (70, 95, 130, 255),
                    width=3 if idx == 0 else 2,
                )
                draw.rounded_rectangle(
                    (ix0 + 16, py0 + 18, ix0 + 128, py1 - 18),
                    radius=10,
                    fill=accent_rgba,
                )
                draw.text(
                    (ix0 + 72, (py0 + py1) // 2),
                    f"CHECK {idx + 1}",
                    font=self.get_font(20),
                    anchor="mm",
                    fill=(10, 15, 25, 255),
                )
                self._draw_outlined_text(
                    draw,
                    (ix0 + 148, (py0 + py1) // 2 - 18),
                    pt[:31],
                    self.get_font(27),
                    fill=(255, 255, 255, 255),
                    outline_color=(0, 0, 0, 230),
                    outline_width=3,
                )

    def _draw_characters_and_subtitle(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        segment: SynthesizedSegment,
        metan_emotion: str,
        zunda_emotion: str,
        mouth_state: str,
    ) -> None:
        """
        左右の四国めたん・ずんだもん立ち絵（どちらも暗くせず常に100%の明るさで表示）と、
        画面下部の話者色分け字幕ボックスを描画する。
        """
        metan_speaking = segment.speaker == "metan"
        zunda_speaking = segment.speaker == "zundamon"

        metan_img = self._load_character_sprite(
            "metan",
            metan_emotion,
            mouth_state if metan_speaking else "close",
        )
        zunda_img = self._load_character_sprite(
            "zundamon",
            zunda_emotion,
            mouth_state if zunda_speaking else "close",
        )

        # しゃべっていない側も暗くせずそのまま表示（発話中の口パク時のみ軽く8pxトークバウンス）
        metan_bounce = 8 if (metan_speaking and mouth_state == "open") else 0
        zunda_bounce = 8 if (zunda_speaking and mouth_state == "open") else 0

        metan_x = -20
        metan_y = self.height - metan_img.height - metan_bounce
        zunda_x = self.width - zunda_img.width + 20
        zunda_y = self.height - zunda_img.height - zunda_bounce

        canvas.alpha_composite(metan_img, (metan_x, metan_y))
        canvas.alpha_composite(zunda_img, (zunda_x, zunda_y))

        # 字幕ボックス描画 (x=315..1605, y=795..1045)
        chars_cfg = self.vv_cfg.get("characters", {})
        spk_cfg = chars_cfg.get(segment.speaker, {})
        display_name = spk_cfg.get(
            "display_name",
            "四国めたん" if metan_speaking else "ずんだもん",
        )
        theme_hex = spk_cfg.get(
            "theme_color", "#FF5C9E" if metan_speaking else "#4CD964"
        )
        dark_hex = spk_cfg.get(
            "border_dark", "#8A1244" if metan_speaking else "#126B24"
        )
        theme_rgba = _hex_to_rgba(theme_hex)
        dark_rgba = _hex_to_rgba(dark_hex)

        sx0, sy0, sx1, sy1 = 315, 795, 1605, 1045
        draw.rounded_rectangle(
            (sx0, sy0, sx1, sy1),
            radius=22,
            fill=(12, 16, 28, 238),
            outline=theme_rgba,
            width=5,
        )

        plate_x0 = sx0 + 28 if metan_speaking else sx1 - 285
        plate_x1 = plate_x0 + 255
        draw.rounded_rectangle(
            (plate_x0, sy0 - 26, plate_x1, sy0 + 28),
            radius=14,
            fill=theme_rgba,
            outline=(255, 255, 255, 230),
            width=2,
        )
        self._draw_outlined_text(
            draw,
            (plate_x0 + 32, sy0 - 17),
            display_name,
            self.get_font(28),
            fill=(255, 255, 255, 255),
            outline_color=dark_rgba,
            outline_width=3,
        )

        wrapped_lines = textwrap.wrap(segment.display_text, width=24)[:3]
        sub_font = self.get_font(42)
        line_spacing = 58
        total_text_h = len(wrapped_lines) * line_spacing
        start_ty = sy0 + 26 + max(10, (sy1 - sy0 - 36 - total_text_h) // 2)

        for idx, wline in enumerate(wrapped_lines):
            ty = start_ty + idx * line_spacing
            draw.text(
                (sx0 + 44, ty),
                wline,
                font=sub_font,
                fill=(255, 255, 255, 255),
                stroke_width=7,
                stroke_fill=dark_rgba,
            )
            draw.text(
                (sx0 + 44, ty),
                wline,
                font=sub_font,
                fill=(255, 255, 255, 255),
                stroke_width=3,
                stroke_fill=theme_rgba,
            )

    def render_segment_frame_pair(
        self,
        episode: GeneratedEpisode,
        segment: SynthesizedSegment,
        total_segments: int,
        metan_emotion: str,
        zunda_emotion: str,
        frames_dir: Path,
    ) -> Tuple[Path, Path]:
        """1セリフにつき「口閉じ (close.png)」と「口開き (open.png)」の2枚を生成する"""
        frames_dir.mkdir(parents=True, exist_ok=True)
        progress = segment.global_index / float(max(1, total_segments))
        total_sections = len(episode.sections)

        paths: List[Path] = []
        for mouth_state in ("close", "open"):
            canvas = self._get_scene_background(segment.slide.diagram_type)
            draw = ImageDraw.Draw(canvas)

            self._draw_slide_card(
                canvas=canvas,
                draw=draw,
                slide=segment.slide,
                section_title=segment.section_title,
                section_index=segment.section_index,
                total_sections=total_sections,
                topic_keyword=episode.topic_keyword,
                category=episode.topic_category,
                progress_ratio=progress,
            )

            self._draw_characters_and_subtitle(
                canvas=canvas,
                draw=draw,
                segment=segment,
                metan_emotion=metan_emotion,
                zunda_emotion=zunda_emotion,
                mouth_state=mouth_state,
            )

            out_path = (
                frames_dir
                / f"seg_{segment.global_index:04d}_{mouth_state}.png"
            )
            canvas.convert("RGB").save(out_path, "PNG", compress_level=1)
            paths.append(out_path)

        return paths[0], paths[1]

    def render_thumbnail(
        self, episode: GeneratedEpisode, out_path: Path
    ) -> Path:
        """1280x720 の高CTRサムネイル画像を自動生成"""
        w, h = self.thumb_w, self.thumb_h
        canvas = Image.new("RGBA", (w, h), (15, 18, 38, 255))
        draw = ImageDraw.Draw(canvas)

        for y in range(h):
            ratio = y / float(h)
            r = int(28 * (1 - ratio) + 85 * ratio)
            g = int(15 * (1 - ratio) + 20 * ratio)
            b = int(55 * (1 - ratio) + 95 * ratio)
            draw.line([(0, y), (w, y)], fill=(r, g, b, 255))

        for x in range(0, w, 90):
            draw.polygon(
                [(x, 0), (x + 35, 0), (w // 2, h // 2)],
                fill=(255, 255, 255, 14),
            )

        draw.rounded_rectangle(
            (225, 125, w - 225, h - 65),
            radius=28,
            fill=(8, 12, 24, 215),
            outline=(255, 205, 45, 255),
            width=5,
        )

        metan_thumb = self._load_character_sprite("metan", "serious", "open")
        zunda_thumb = self._load_character_sprite(
            "zundamon", "surprised", "open"
        )
        canvas.alpha_composite(metan_thumb, (-75, h - metan_thumb.height + 10))
        canvas.alpha_composite(
            zunda_thumb, (w - zunda_thumb.width + 65, h - zunda_thumb.height + 10)
        )

        badge_text = episode.metadata.thumb_badge_text or "徹底考察"
        draw.rounded_rectangle(
            (36, 26, 330, 104),
            radius=16,
            fill=(230, 35, 65, 255),
            outline=(255, 255, 255, 255),
            width=4,
        )
        self._draw_outlined_text(
            draw,
            (68, 39),
            badge_text[:6],
            self.get_font(44),
            fill=(255, 255, 255, 255),
            outline_color=(80, 0, 15, 255),
            outline_width=4,
        )

        upper_text = episode.metadata.thumb_upper_text[:10]
        font_upper = self.get_font(68)
        draw.text(
            (w // 2, 215),
            upper_text,
            font=font_upper,
            anchor="mm",
            fill=(255, 230, 45, 255),
            stroke_width=10,
            stroke_fill=(0, 0, 0, 255),
        )

        kw_short = episode.topic_keyword[:15]
        draw.rounded_rectangle(
            (250, 305, w - 250, 395),
            radius=14,
            fill=(255, 45, 115, 245),
        )
        draw.text(
            (w // 2, 350),
            kw_short,
            font=self.get_font(46),
            anchor="mm",
            fill=(255, 255, 255, 255),
            stroke_width=5,
            stroke_fill=(60, 0, 25, 255),
        )

        main_text = episode.metadata.thumb_main_text[:9]
        font_main = self.get_font(86)
        draw.text(
            (w // 2, 515),
            main_text,
            font=font_main,
            anchor="mm",
            fill=(0, 245, 212, 255),
            stroke_width=14,
            stroke_fill=(0, 0, 0, 255),
        )
        draw.text(
            (w // 2, 515),
            main_text,
            font=font_main,
            anchor="mm",
            fill=(255, 255, 255, 255),
            stroke_width=5,
            stroke_fill=(0, 110, 160, 255),
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(out_path, "PNG")
        logger.info("サムネイル画像を生成しました: %s", out_path)
        return out_path
