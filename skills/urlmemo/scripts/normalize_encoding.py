#!/usr/bin/env python3
"""
文字エンコードを UTF-8 に統一するユーティリティ。

to_utf8(data, declared_charset=None) -> (text: str, detected_charset: str)

- data が bytes: BOM 判定 → 宣言 charset → charset-normalizer/chardet →
  日本語フォールバック → utf-8(errors='replace') の順でデコード。
- data が str: 既にデコード済みとして採用（web_extract がテキストを返すケース）。
- 共通後処理: BOM 文字除去 / Unicode NFC 正規化 / 改行を \n に統一。

外部ライブラリ（charset-normalizer, chardet）は任意。無くても内蔵フォールバックで動く。
"""

import unicodedata

# 日本語ページで遭遇しやすい順
_FALLBACK_CHARSETS = ["utf-8", "cp932", "shift_jis", "euc-jp", "iso-2022-jp", "latin-1"]


def _detect_charset(data: bytes):
    """利用可能な検出ライブラリで charset を推定。無ければ None。"""
    try:
        from charset_normalizer import from_bytes  # type: ignore

        best = from_bytes(data).best()
        if best is not None and best.encoding:
            return best.encoding
    except Exception:
        pass
    try:
        import chardet  # type: ignore

        guess = chardet.detect(data)
        if guess and guess.get("encoding"):
            return guess["encoding"]
    except Exception:
        pass
    return None


def _strip_bom(text: str) -> str:
    return text.lstrip("﻿")


def _post_process(text: str) -> str:
    text = _strip_bom(text)
    text = unicodedata.normalize("NFC", text)
    # 改行統一
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _decode_bytes(data: bytes, declared_charset=None):
    # BOM 優先判定
    if data[:3] == b"\xef\xbb\xbf":
        return data.decode("utf-8-sig"), "utf-8"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"

    tried = []
    # 1. 宣言された charset を最優先
    if declared_charset:
        tried.append(declared_charset)
    # 2. 検出ライブラリ
    detected = _detect_charset(data)
    if detected:
        tried.append(detected)
    # 3. フォールバック
    tried.extend(_FALLBACK_CHARSETS)

    seen = set()
    for enc in tried:
        if not enc:
            continue
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue

    # 4. 最終手段
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def to_utf8(data, declared_charset=None):
    """data(bytes|str) を UTF-8 文字列に正規化して (text, detected_charset) を返す。"""
    if isinstance(data, str):
        return _post_process(data), "utf-8(str)"
    if isinstance(data, (bytes, bytearray)):
        text, enc = _decode_bytes(bytes(data), declared_charset)
        return _post_process(text), enc
    # それ以外は文字列化
    return _post_process(str(data)), "utf-8(coerced)"


if __name__ == "__main__":
    import sys

    raw = sys.stdin.buffer.read()
    text, enc = to_utf8(raw)
    sys.stderr.write(f"detected charset: {enc}\n")
    sys.stdout.write(text)
