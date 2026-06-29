# pdf_stamp.py  v3 (fixed full version)
# =====================================
# PDF に Stamp Annotation を押すツール。
# Acrobat / Just PDF 互換：注釈オブジェクトとして選択・移動可能。

import sys
import os
import io
import configparser
from datetime import datetime
from copy import deepcopy

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    DictionaryObject, NameObject, NumberObject,
    RectangleObject, DecodedStreamObject, create_string_object
)
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.units import mm
import fitz  # PyMuPDF
from PIL import Image  # Pillow（JPEG2000書き出し用。PyMuPDF単体ではJP2出力不可）


# ─────────────────────────────────────────────────────────
#  設定読み込み
# ─────────────────────────────────────────────────────────

def load_config(ini_path: str) -> configparser.RawConfigParser:
    cfg = configparser.RawConfigParser()
    cfg.read(ini_path, encoding="utf-8")
    return cfg


def cfg_get(cfg, key, fallback):
    return cfg.get("stamp", key, fallback=str(fallback))


def parse_bool(val: str) -> bool:
    return val.strip().lower() in ("true", "yes", "1", "on")


def hex_to_rgb(hex_str: str):
    h = hex_str.strip().lstrip("#")
    return int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0


def _pdf_datetime(dt: datetime) -> str:
    """datetime → PDF 日時文字列  例: D:20260607132045+09'00'"""
    if dt.tzinfo is None:
        from datetime import timezone
        local_offset = datetime.now(timezone.utc).astimezone().utcoffset()
        dt = dt.replace(tzinfo=timezone(local_offset))
    offset = dt.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm_ = divmod(abs(total_minutes), 60)
    return dt.strftime(f"D:%Y%m%d%H%M%S{sign}{hh:02d}'{mm_:02d}'")


# ─────────────────────────────────────────────────────────
#  フォント登録
# ─────────────────────────────────────────────────────────

_FONT_FILE_MAP = {
    "meiryo": ("meiryo.ttc", 0),
    "meiryobold": ("meiryob.ttc", 0),
    "meiryoui": ("meiryo.ttc", 1),
    "meiryouibold": ("meiryob.ttc", 1),
    "msgothic": ("msgothic.ttc", 0),
    "msuigothic": ("msgothic.ttc", 1),
    "msmincho": ("msmincho.ttc", 0),
    "msuimincho": ("msmincho.ttc", 1),
    "yugothic": ("YuGothR.ttc", 0),
    "yugothicbold": ("YuGothB.ttc", 0),
    "yumincho": ("yumin.ttf", None),
    "arial": ("arial.ttf", None),
    "arialbold": ("arialbd.ttf", None),
    "timesnewroman": ("times.ttf", None),
    "timesnewromanbold": ("timesbd.ttf", None),
}

_WIN_FONT_DIRS = [
    r"C:\Windows\Fonts",
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "Microsoft", "Windows", "Fonts"),
]

_registered_fonts = {}


def _normalize_font_name(name: str) -> str:
    return name.lower().replace(" ", "").replace("-", "").replace("_", "")


def resolve_font(font_name: str) -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    BUILTIN = {
        "helvetica", "helvetica-bold", "helvetica-oblique", "helvetica-boldoblique",
        "times-roman", "times-bold", "times-italic", "times-bolditalic",
        "courier", "courier-bold", "courier-oblique", "courier-boldoblique",
    }
    if font_name.lower() in BUILTIN:
        return font_name

    key = _normalize_font_name(font_name)

    if key in _registered_fonts:
        return _registered_fonts[key]

    if key not in _FONT_FILE_MAP:
        print(f"[フォント警告] '{font_name}' は未対応 → Helvetica-Bold")
        return "Helvetica-Bold"

    ttf_file, ttc_index = _FONT_FILE_MAP[key]

    font_path = None
    for d in _WIN_FONT_DIRS:
        candidate = os.path.join(d, ttf_file)
        if os.path.isfile(candidate):
            font_path = candidate
            break

    if font_path is None:
        print(f"[フォント警告] {ttf_file} が見つからない → Helvetica-Bold")
        return "Helvetica-Bold"

    rl_name = f"_stamp_{key}"
    try:
        if ttc_index is not None:
            pdfmetrics.registerFont(TTFont(rl_name, font_path, subfontIndex=ttc_index))
        else:
            pdfmetrics.registerFont(TTFont(rl_name, font_path))
        _registered_fonts[key] = rl_name
        return rl_name
    except Exception:
        return "Helvetica-Bold"


# ─────────────────────────────────────────────────────────
#  スタンプの実描画領域
# ─────────────────────────────────────────────────────────

def get_content_box(stamp_reader: PdfReader):
    p = stamp_reader.pages[0]
    for key in ("/CropBox", "/ArtBox", "/TrimBox"):
        box = p.get(key)
        if box:
            if hasattr(box, "get_object"):
                box = box.get_object()
            v = [float(x) for x in box]
            return v[0], v[1], v[2], v[3]
    mb = p.mediabox
    return float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)


# ─────────────────────────────────────────────────────────
#  リソース統合
# ─────────────────────────────────────────────────────────

def merge_font_resources(base_res: DictionaryObject, add_res: DictionaryObject, writer: PdfWriter):
    add_fonts = add_res.get("/Font", {})
    if hasattr(add_fonts, "get_object"):
        add_fonts = add_fonts.get_object()
    if not add_fonts:
        return

    if NameObject("/Font") not in base_res:
        base_res[NameObject("/Font")] = DictionaryObject()

    bf = base_res[NameObject("/Font")]
    if hasattr(bf, "get_object"):
        bf = bf.get_object()

    for k, v in add_fonts.items():
        nk = k
        while NameObject(nk) in bf:
            nk = "/d_" + nk.lstrip("/")
        bf[NameObject(nk)] = writer._add_object(deepcopy(v.get_object()))


# ─────────────────────────────────────────────────────────
#  日付レイヤー生成
# ─────────────────────────────────────────────────────────

def make_date_layer(w, h, date_str, font_size, color_hex, y_ratio, font_name):
    rl_font = resolve_font(font_name)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(w, h))
    r, g, b = hex_to_rgb(color_hex)
    c.setFillColorRGB(r, g, b)
    c.setFont(rl_font, font_size)
    tw = c.stringWidth(date_str, rl_font, font_size)
    x = (w - tw) / 2
    y = h * y_ratio - font_size / 2
    c.drawString(x, y, date_str)
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────
#  FormXObject（スタンプ本体）
# ─────────────────────────────────────────────────────────

def build_form_xobject(
    writer, stamp_page, content_box,
    date_str, date_font_size, date_color_hex, date_y_ratio, date_font
):
    cx0, cy0, cx1, cy1 = content_box
    content_w = cx1 - cx0
    content_h = cy1 - cy0

    raw_stamp = stamp_page["/Contents"].get_object().get_data()
    stamp_res = stamp_page.get("/Resources", DictionaryObject())
    if hasattr(stamp_res, "get_object"):
        stamp_res = stamp_res.get_object()
    merged_res = deepcopy(stamp_res)

    form_stream = (
        f"q 1 0 0 1 {-cx0:.6f} {-cy0:.6f} cm\n".encode()
        + raw_stamp
        + b"\nQ"
    )

    if date_str:
        date_pdf = make_date_layer(
            content_w, content_h, date_str,
            date_font_size, date_color_hex, date_y_ratio, date_font
        )
        date_reader = PdfReader(io.BytesIO(date_pdf))
        date_page = date_reader.pages[0]
        raw_date = date_page["/Contents"].get_object().get_data()
        date_res = date_page.get("/Resources", DictionaryObject())
        if hasattr(date_res, "get_object"):
            date_res = date_res.get_object()
        merge_font_resources(merged_res, date_res, writer)
        form_stream += b"\n" + raw_date

    xobj = DecodedStreamObject()
    xobj.set_data(form_stream)
    xobj[NameObject("/Type")] = NameObject("/XObject")
    xobj[NameObject("/Subtype")] = NameObject("/Form")
    xobj[NameObject("/FormType")] = NumberObject(1)
    xobj[NameObject("/BBox")] = RectangleObject((0, 0, content_w, content_h))
    xobj[NameObject("/Resources")] = merged_res

    return writer._add_object(xobj)


# ─────────────────────────────────────────────────────────
#  回転版 FormXObject
# ─────────────────────────────────────────────────────────

def build_rotated_form(writer, src_ref, w, h, angle):
    form = DecodedStreamObject()

    if angle == 90:
        matrix = f"0 1 -1 0 {h} 0 cm"
        bbox = RectangleObject((0, 0, h, w))
    elif angle == 180:
        matrix = f"-1 0 0 -1 {w} {h} cm"
        bbox = RectangleObject((0, 0, w, h))
    elif angle == 270:
        matrix = f"0 -1 1 0 0 {w} cm"
        bbox = RectangleObject((0, 0, h, w))
    else:
        raise ValueError(angle)

    stream = (
        f"q {matrix}\n"
        f"/Fm0 Do\n"
        f"Q"
    ).encode()

    form.set_data(stream)
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/FormType")] = NumberObject(1)
    form[NameObject("/BBox")] = bbox

    res = DictionaryObject()
    xobjs = DictionaryObject()
    xobjs[NameObject("/Fm0")] = src_ref
    res[NameObject("/XObject")] = xobjs
    form[NameObject("/Resources")] = res

    return writer._add_object(form)
# ─────────────────────────────────────────────────────────
#  メイン処理（スタンプ押印）
# ─────────────────────────────────────────────────────────

def stamp_pdf(target_path: str, cfg: configparser.RawConfigParser) -> str:
    base_dir = os.path.dirname(os.path.abspath(target_path))

    stamp_file     = cfg_get(cfg, "stamp_file",    "Stamp.pdf")
    # Stamp.pdf 検索: ① target と同フォルダー ② script/EXE と同フォルダー
    _script_dir = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                   else os.path.dirname(os.path.abspath(__file__)))
    stamp_path = (os.path.join(base_dir, stamp_file)
                  if os.path.isfile(os.path.join(base_dir, stamp_file))
                  else os.path.join(_script_dir, stamp_file))

    stamp_w_mm     = float(cfg_get(cfg, "stamp_width",    30))
    stamp_h_mm_cfg = float(cfg_get(cfg, "stamp_height",    0))  # 0 = アスペクト比自動

    offset_x_mm    = float(cfg_get(cfg, "offset_x",       10))
    offset_y_mm    = float(cfg_get(cfg, "offset_y",       10))

    show_date      = parse_bool(cfg_get(cfg, "show_date",   "true"))
    date_fmt       = cfg_get(cfg, "date_format",  "%Y/%m/%d")
    date_fsize     = float(cfg_get(cfg, "date_font_size",    8))
    date_color     = cfg_get(cfg, "date_color",   "#CC0000")
    date_y_ratio   = float(cfg_get(cfg, "date_y_ratio",  0.445))
    date_font      = cfg_get(cfg, "date_font", "Helvetica-Bold")

    annot_author   = cfg_get(cfg, "author",   "")
    annot_subject  = cfg_get(cfg, "subject",  "")
    annot_stamp_id = cfg_get(cfg, "stamp_id", "Draft")

    suffix         = cfg_get(cfg, "output_suffix", "_stamped")
    overwrite      = parse_bool(cfg_get(cfg, "overwrite",  "false"))

    if not os.path.isfile(stamp_path):
        raise FileNotFoundError(f"スタンプファイルが見つかりません: {stamp_path}")

    # スタンプPDF読み込み
    stamp_reader = PdfReader(stamp_path)
    cx0, cy0, cx1, cy1 = get_content_box(stamp_reader)
    content_w = cx1 - cx0
    content_h = cy1 - cy0

    stamp_w_pt  = stamp_w_mm * mm
    stamp_h_pt  = stamp_h_mm_cfg * mm if stamp_h_mm_cfg > 0 else stamp_w_pt * (content_h / content_w)
    offset_x_pt = offset_x_mm * mm
    offset_y_pt = offset_y_mm * mm

    now          = datetime.now().astimezone()
    date_str     = now.strftime(date_fmt) if show_date else None
    pdf_date_str = _pdf_datetime(now)

    writer = PdfWriter()

    # スタンプPDFを writer に追加（リソース保持のため）
    writer.append(stamp_reader)
    stamp_page_in_writer = writer.pages[0]
    stamp_page_count = len(stamp_reader.pages)

    # スタンプ本体 XObject
    xobj_ref = build_form_xobject(
        writer,
        stamp_page_in_writer,
        (cx0, cy0, cx1, cy1),
        date_str, date_fsize, date_color, date_y_ratio, date_font,
    )

    # 回転版 XObject
    xobj_ref_r90  = build_rotated_form(writer, xobj_ref, content_w, content_h, 90)
    xobj_ref_r180 = build_rotated_form(writer, xobj_ref, content_w, content_h, 180)
    xobj_ref_r270 = build_rotated_form(writer, xobj_ref, content_w, content_h, 270)

    stamp_name_value = "/" + annot_stamp_id.replace(" ", "#20")

    # 対象PDFを追加
    target_reader = PdfReader(target_path)
    offset = stamp_page_count
    writer.append(target_reader)

    # 各ページにスタンプ押印
    for i, page in enumerate(writer.pages[offset:]):
        pw = float(page.mediabox.width)
        ph = float(page.mediabox.height)

        page_rotate = int(page.get("/Rotate", 0)) % 360
        effective_rotate = page_rotate  # スタンプはページの向きに完全追従

        # 回転に応じてスタンプの幅・高さを入れ替え
        if effective_rotate in (90, 270):
            rw = stamp_h_pt
            rh = stamp_w_pt
        else:
            rw = stamp_w_pt
            rh = stamp_h_pt

        # 右上基準の座標計算
        if page_rotate == 90:
            x0 = offset_y_pt
            y0 = ph - offset_x_pt - rh
        elif page_rotate == 180:
            x0 = offset_x_pt
            y0 = offset_y_pt
        elif page_rotate == 270:
            x0 = pw - offset_y_pt - rw
            y0 = offset_x_pt
        else:  # 0°
            x0 = pw - offset_x_pt - rw
            y0 = ph - offset_y_pt - rh

        x1 = x0 + rw
        y1 = y0 + rh

        # ページの回転に合わせて XObject を選択
        if effective_rotate == 90:
            ap_ref = xobj_ref_r90
        elif effective_rotate == 180:
            ap_ref = xobj_ref_r180
        elif effective_rotate == 270:
            ap_ref = xobj_ref_r270
        else:
            ap_ref = xobj_ref

        ap_dict = DictionaryObject()
        ap_dict[NameObject("/N")] = ap_ref

        annot = DictionaryObject()
        annot[NameObject("/Type")]        = NameObject("/Annot")
        annot[NameObject("/Subtype")]     = NameObject("/Stamp")
        annot[NameObject("/Name")]        = NameObject(stamp_name_value)
        annot[NameObject("/Rect")]        = RectangleObject((x0, y0, x1, y1))
        annot[NameObject("/AP")]          = ap_dict
        annot[NameObject("/F")]           = NumberObject(4)
        annot[NameObject("/T")]           = create_string_object(annot_author)
        annot[NameObject("/Subj")]        = create_string_object(annot_subject)
        annot[NameObject("/Contents")]    = create_string_object(annot_subject)
        annot[NameObject("/CreationDate")] = create_string_object(pdf_date_str)
        annot[NameObject("/M")]            = create_string_object(pdf_date_str)

        writer.add_annotation(page_number=offset + i, annotation=annot)

    # スタンプ用ページを削除
    for _ in range(stamp_page_count):
        writer.remove_page(0)

    base, ext = os.path.splitext(target_path)
    out_path = target_path if overwrite else base + suffix + ext

    with open(out_path, "wb") as f:
        writer.write(f)

    return out_path
# ═══════════════════════════════════════════════════════════
#  クリーンアップ処理（PyMuPDF）
# ═══════════════════════════════════════════════════════════

def cleanup_pdf(src_path: str, out_path: str) -> None:
    """
    PyMuPDF でクリーンアップして out_path に保存する。
      ・ブックマーク削除
      ・レイヤー統合（OCProperties 削除）
      ・メタデータ完全削除（Info / XMP）
      ・表示設定リセット（SinglePage / UseNone）
    """
    doc = fitz.open(src_path)
    root = doc.pdf_catalog()

    # ブックマーク削除
    doc.set_toc([])
    doc.xref_set_key(root, "Outlines", "null")
    doc.xref_set_key(root, "Dests", "null")
    doc.xref_set_key(root, "PageLabels", "null")

    # レイヤー統合
    doc.bake(annots=False, widgets=True)
    _, ocprops = doc.xref_get_key(root, "OCProperties")
    if ocprops and ocprops != "null":
        doc.xref_set_key(root, "OCProperties", "null")

    # メタデータ削除
    doc.set_metadata({})
    _, info = doc.xref_get_key(root, "Info")
    if info and info != "null":
        doc.xref_set_key(root, "Info", "null")
    doc.set_xml_metadata("")
    _, meta = doc.xref_get_key(root, "Metadata")
    if meta and meta != "null":
        doc.xref_set_key(root, "Metadata", "null")

    # 表示設定リセット
    doc.set_pagelayout("SinglePage")
    doc.set_pagemode("UseNone")

    try:
        doc.save(out_path, garbage=4, deflate=True, clean=True, incremental=False)
    finally:
        doc.close()


# ═══════════════════════════════════════════════════════════
#  画像圧縮処理（PyMuPDF + Pillow）
# ═══════════════════════════════════════════════════════════

# 圧縮レベルのプリセット（メニュー [3][4] 選択後のサブメニューで使用）
#   JPEG     : jpeg_quality (1-100、大きいほど高画質・低圧縮)
#   JPEG2000 : jp2_rate     (おおよその圧縮比。大きいほど高圧縮・低画質)
COMPRESS_PRESETS = {
    "1": {"label": "JPEG 低圧縮（高画質）",
          "kwargs": {"image_format": "jpeg", "jpeg_quality": 85}},
    "2": {"label": "JPEG 標準",
          "kwargs": {"image_format": "jpeg", "jpeg_quality": 60}},
    "3": {"label": "JPEG 高圧縮（低画質）",
          "kwargs": {"image_format": "jpeg", "jpeg_quality": 30}},
    "4": {"label": "JPEG2000 低圧縮（高画質）",
          "kwargs": {"image_format": "jp2", "jp2_rate": 10}},
    "5": {"label": "JPEG2000 標準",
          "kwargs": {"image_format": "jp2", "jp2_rate": 20}},
    "6": {"label": "JPEG2000 高圧縮（低画質）",
          "kwargs": {"image_format": "jp2", "jp2_rate": 40}},
}


def compress_images_pdf(src_path: str, out_path: str, image_format: str = "jpeg",
                         jpeg_quality: int = 60, jp2_rate: float = 20) -> dict:
    """
    PDF 内のビットマップ画像を再圧縮し、out_path に保存する。
      image_format: "jpeg" または "jp2"（JPEG2000、Pillow/OpenJPEG経由）
      ・ベクター画像／テキストには影響しない
      ・透過（アルファ／マスク）を持つ画像は情報が失われるためスキップ
      ・極小画像（64px未満、区切り線やマスク等）はスキップ
      ・再圧縮しても元より大きくなる場合はスキップ
    戻り値: {"compressed": 圧縮した画像数, "skipped": スキップした画像数}
    """
    MIN_DIM = 64  # これより小さい画像はスキップ（JPEG2000の制約に合わせて統一）

    doc = fitz.open(src_path)
    seen_xrefs = set()
    n_compressed = 0
    n_skipped = 0

    try:
        for page in doc:
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)

                try:
                    base_image = doc.extract_image(xref)
                except Exception:
                    n_skipped += 1
                    continue

                if not base_image or not base_image.get("image"):
                    n_skipped += 1
                    continue

                # 透過（アルファ／ソフトマスク）画像はJPEG/JP2化すると
                # 透明情報が失われるためスキップ
                if base_image.get("smask", 0):
                    n_skipped += 1
                    continue

                # 極小画像（区切り線・マスク等）はスキップ
                if base_image.get("width", 0) < MIN_DIM or base_image.get("height", 0) < MIN_DIM:
                    n_skipped += 1
                    continue

                image_bytes = base_image["image"]

                try:
                    pix = fitz.Pixmap(image_bytes)

                    # アルファチャンネルを除去
                    if pix.alpha:
                        pix = fitz.Pixmap(pix, 0)

                    # Gray/RGB以外の色空間（CMYK等）はRGBに変換
                    if pix.colorspace is None or pix.colorspace.n not in (1, 3):
                        pix = fitz.Pixmap(fitz.csRGB, pix)

                    if image_format == "jp2":
                        # PyMuPDFはJPEG2000の書き出しに対応していないため
                        # Pillow（OpenJPEG）で再圧縮する
                        mode = "L" if pix.n == 1 else "RGB"
                        pil_img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
                        buf = io.BytesIO()
                        pil_img.save(
                            buf, format="JPEG2000",
                            quality_mode="rates", quality_layers=[jp2_rate],
                        )
                        new_bytes = buf.getvalue()
                    else:
                        new_bytes = pix.tobytes("jpeg", jpg_quality=jpeg_quality)
                except Exception:
                    n_skipped += 1
                    continue

                # 再圧縮しても元より大きくなる場合は何もしない
                if len(new_bytes) >= len(image_bytes):
                    n_skipped += 1
                    continue

                try:
                    page.replace_image(xref, stream=new_bytes)
                    n_compressed += 1
                except Exception:
                    n_skipped += 1
                    continue

        doc.save(out_path, garbage=4, deflate=True, clean=True, incremental=False)
    finally:
        doc.close()

    return {"compressed": n_compressed, "skipped": n_skipped}


# ═══════════════════════════════════════════════════════════
#  EXE ディレクトリ取得（PyInstaller / Nuitka / .py 共通）
# ═══════════════════════════════════════════════════════════

def get_exe_dir() -> str:
    """
    実行ファイルのディレクトリを返す。
    PyInstaller onefile / onedir、Nuitka standalone、通常 .py すべてに対応。
    """
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller onefile、および onedir + --contents-directory 使用時:
        # _MEIPASS は展開先(onefile)や _internal フォルダ(onedir)を指すため、
        # EXE本体の実際の場所は argv[0] から取得する
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        # PyInstaller onedir（旧仕様） / Nuitka standalone
        # （sys.executable の拡張子では判定しない: 通常の .py 実行でも
        #   Windows では python.exe が .exe 拡張子のため誤判定してしまう）
        return os.path.dirname(os.path.abspath(sys.executable))
    # 通常 .py スクリプト
    return os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════════
#  ini 検索
# ═══════════════════════════════════════════════════════════

def find_ini(target_path: str) -> str:
    """stamp.ini を探す: target と同フォルダー → EXE と同フォルダー"""
    for base in (os.path.dirname(os.path.abspath(target_path)), get_exe_dir()):
        p = os.path.join(base, "stamp.ini")
        if os.path.isfile(p):
            return p
    return os.path.join(get_exe_dir(), "stamp.ini")


# ═══════════════════════════════════════════════════════════
#  必要ファイルチェック
# ═══════════════════════════════════════════════════════════

def check_requirements(target_path: str) -> list:
    errors = []

    ini_path = find_ini(target_path)
    if not os.path.isfile(ini_path):
        errors.append(
            "[設定ファイル未検出] stamp.ini が見つかりません。\n"
            f"  探した場所①: {os.path.dirname(os.path.abspath(target_path))}\n"
            f"  探した場所②: {get_exe_dir()}\n"
            "  → stamp.ini を EXE と同じフォルダーに置いてください。"
        )
        return errors

    cfg = load_config(ini_path)
    stamp_file      = cfg_get(cfg, "stamp_file", "Stamp.pdf")
    target_dir      = os.path.dirname(os.path.abspath(target_path))
    exe_dir         = get_exe_dir()

    stamp_in_target = os.path.join(target_dir, stamp_file)
    stamp_in_exe    = os.path.join(exe_dir,    stamp_file)

    if not os.path.isfile(stamp_in_target) and not os.path.isfile(stamp_in_exe):
        errors.append(
            f"[スタンプファイル未検出] {stamp_file} が見つかりません。\n"
            f"  探した場所①: {target_dir}\n"
            f"  探した場所②: {exe_dir}\n"
            f"  → {stamp_file} を EXE と同じフォルダーに置いてください。"
        )

    return errors


# ═══════════════════════════════════════════════════════════
#  メイン
# ═══════════════════════════════════════════════════════════

def main():
    base_dir = get_exe_dir()

    import traceback

    def logprint(msg=""):
        print(msg)

    logprint(f"=== pdf_stamp 起動 {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    logprint(f"argv: {sys.argv}")

    if len(sys.argv) < 2:
        logprint("使い方: pdf_stamp.py <file1.pdf> [file2.pdf ...]")
        logprint("        （PDF をドラッグ＆ドロップしてください）")
        input("\nEnter で終了...")
        sys.exit(0)

    for t in sys.argv[1:]:
        if not os.path.isfile(t):
            logprint(f"[スキップ] ファイルが存在しません: {t}")
            continue
        if not t.lower().endswith(".pdf"):
            logprint(f"[スキップ] PDF ではありません: {t}")
            continue

        errs = check_requirements(t)
        if errs:
            logprint(f"[処理中止] {os.path.basename(t)}")
            for msg in errs:
                logprint(msg)
            continue

        ini = find_ini(t)
        logprint(f"[設定] stamp.ini: {ini}")
        cfg = load_config(ini)

        print()
        print(f"  ファイル: {os.path.basename(t)}")
        print()
        print("  [1] スタンプ押印のみ")
        print("  [2] クリーンアップのみ")
        print("  [3] 画像圧縮のみ")
        print("  [4] スタンプ押印 + クリーンアップ + 画像圧縮")
        print("  [5] スタンプ押印 + クリーンアップ  ← デフォルト")
        print("  [0] スキップ")
        print()

        while True:
            choice = input("  選択してください (0-5) [Enter=5]: ").strip()
            if choice == "":
                choice = "5"
                break
            if choice in ("0", "1", "2", "3", "4", "5"):
                break
            print("  → 0〜5 の数字を入力してください。")

        if choice == "0":
            logprint(f"[スキップ] {os.path.basename(t)}")
            continue

        do_stamp    = choice in ("1", "4", "5")
        do_cleanup  = choice in ("2", "4", "5")
        do_compress = choice in ("3", "4")

        compress_preset = None
        if do_compress:
            print()
            print("  圧縮方式・圧縮率を選択してください:")
            print("    [1] JPEG      低圧縮（高画質）")
            print("    [2] JPEG      標準")
            print("    [3] JPEG      高圧縮（低画質）")
            print("    [4] JPEG2000  低圧縮（高画質）")
            print("    [5] JPEG2000  標準  ← デフォルト")
            print("    [6] JPEG2000  高圧縮（低画質）")
            print()

            while True:
                q_choice = input("  選択してください (1-6) [Enter=5]: ").strip()
                if q_choice == "":
                    q_choice = "5"
                    break
                if q_choice in ("1", "2", "3", "4", "5", "6"):
                    break
                print("  → 1〜6 の数字を入力してください。")

            compress_preset = COMPRESS_PRESETS[q_choice]

        try:
            suffix     = cfg_get(cfg, "output_suffix", "_stamped")
            base, ext  = os.path.splitext(t)
            out_path   = base + suffix + ext

            if do_stamp:
                stamped = stamp_pdf(t, cfg)
                logprint(f"[スタンプ完了] → {os.path.basename(stamped)}")
                work_path = stamped
            else:
                work_path = t

            if do_cleanup:
                cleanup_out = out_path
                if os.path.abspath(work_path) == os.path.abspath(cleanup_out):
                    tmp_path = cleanup_out + ".tmp"
                    cleanup_pdf(work_path, tmp_path)
                    os.replace(tmp_path, cleanup_out)
                else:
                    cleanup_pdf(work_path, cleanup_out)
                logprint(f"[クリーンアップ完了] → {os.path.basename(cleanup_out)}")
                work_path = cleanup_out

            if do_compress:
                if do_stamp or do_cleanup:
                    # スタンプ／クリーンアップ済みのファイルに上書きして圧縮を反映
                    compress_dst = out_path
                else:
                    # 画像圧縮のみ：専用サフィックスで別名保存
                    compress_suffix = cfg_get(cfg, "compress_suffix", "_compressed")
                    cbase, cext      = os.path.splitext(t)
                    compress_dst     = cbase + compress_suffix + cext

                before_size = os.path.getsize(work_path)
                tmp_path    = compress_dst + ".tmp"

                stats = compress_images_pdf(work_path, tmp_path, **compress_preset["kwargs"])
                after_size = os.path.getsize(tmp_path)

                if after_size >= before_size:
                    # 圧縮しても元のサイズ以下にならない場合はスキップし、元のファイルを維持する
                    os.remove(tmp_path)
                    logprint(
                        f"[画像圧縮スキップ] 圧縮しても元のサイズ以下にならないため、圧縮は行いませんでした "
                        f"（{before_size / 1024:.1f}KB → {after_size / 1024:.1f}KB）"
                    )
                    # work_path は変更しない（圧縮前の状態を最終結果として維持）
                else:
                    os.replace(tmp_path, compress_dst)
                    logprint(
                        f"[画像圧縮完了({compress_preset['label']})] → {os.path.basename(compress_dst)} "
                        f"（圧縮 {stats['compressed']} 件 / スキップ {stats['skipped']} 件）"
                    )
                    logprint(
                        f"  サイズ: {before_size / 1024:.1f}KB → {after_size / 1024:.1f}KB "
                        f"（{(1 - after_size / before_size) * 100:.1f}% 削減）"
                    )
                    work_path = compress_dst

            logprint(f"[完了] {os.path.basename(t)} → {os.path.basename(work_path)}")

        except PermissionError:
            logprint("[エラー] 保存できませんでした。")
            logprint("  → 出力先ファイルが開かれています。閉じて再実行してください。")
        except Exception as e:
            logprint(f"[エラー] {os.path.basename(t)}: {e}")
            logprint(traceback.format_exc())

    logprint("=== 終了 ===")

    input("\nEnter で終了...")


if __name__ == "__main__":
    main()
