"""
Signal Parser - Free, local image parsing using EasyOCR + regex.
No API key required. Handles both Telegram signal formats.

Format 1 (compact): #SENSEX 77400CE buy abv 213 tar 249 291 sl 181
Format 2 (structured): BUY / LAURUSLABS APR 1100PE / ENTRY - 44 / SL - 36 / RISK - 7000
"""

import re
import tempfile
from pathlib import Path
from loguru import logger
from PIL import Image, ImageOps, ImageFilter

# EasyOCR is loaded once (lazy) to avoid startup delay
_ocr_reader = None


def is_ocr_available() -> bool:
    """Check if EasyOCR is available and loaded"""
    global _ocr_reader
    if _ocr_reader is not None:
        return True
    try:
        _get_reader()
        return _ocr_reader is not None
    except Exception as e:
        logger.warning(f"EasyOCR check failed: {e}")
        return False


def _get_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr

        logger.info("Loading EasyOCR model (first time may take 30 seconds)...")
        _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        logger.info("EasyOCR ready")
    return _ocr_reader


def _prepare_ocr_variants(image_path: str) -> list[str]:
    """Create OCR-friendly image variants for noisy screenshots."""
    temp_paths: list[str] = []
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)

        # Upscale + grayscale + autocontrast helps on compressed Telegram screenshots.
        processed = img.convert("L")
        processed = processed.resize(
            (processed.width * 2, processed.height * 2), Image.Resampling.LANCZOS
        )
        processed = ImageOps.autocontrast(processed)
        processed = processed.filter(ImageFilter.SHARPEN)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            processed.save(tmp.name, format="PNG")
            temp_paths.append(tmp.name)
    except Exception as e:
        logger.debug(f"Could not create OCR variants: {e}")
    return temp_paths


def _extract_text_from_image(image_path: str) -> str:
    """Use EasyOCR to extract text from a Telegram screenshot with fallbacks."""
    import os

    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")

    reader = _get_reader()
    variants = [image_path, *_prepare_ocr_variants(image_path)]
    lines: list[str] = []
    seen: set[str] = set()

    try:
        for variant in variants:
            for paragraph in (True, False):
                try:
                    results = reader.readtext(variant, detail=0, paragraph=paragraph)
                    for item in results:
                        cleaned = str(item).strip()
                        key = cleaned.upper()
                        if cleaned and key not in seen:
                            seen.add(key)
                            lines.append(cleaned)
                except Exception as e:
                    logger.debug(f"OCR read failed for {variant} paragraph={paragraph}: {e}")
    finally:
        for variant in variants[1:]:
            try:
                os.remove(variant)
            except OSError:
                pass

    text = "\n".join(lines)
    if not text.strip():
        raise ValueError("No text detected in image")
    logger.debug(f"OCR extracted:\n{text}")
    return text


def _normalize(text: str) -> str:
    """Normalize common abbreviations and symbols"""
    text = text.upper().strip()
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    replacements = {
        "ABV": "ABOVE",
        "BLW": "BELOW",
        "BTW": "BELOW",
        "TAR": "TARGET",
        "TGT": "TARGET",
        "TRG": "TARGET",
        "TG": "TARGET",
        "T1": "TARGET1",
        "T2": "TARGET2",
        "T3": "TARGET3",
        "SLT": "SL",
        "S/L": "SL",
        "S L": "SL",
        "STOPLOSS": "SL",
        "STOP LOSS": "SL",
        "STOP-LOSS": "SL",
        "ENT": "ENTRY",
        "ENTER": "ENTRY",
        "CE ": "CE ",
        "PE ": "PE ",
        "@": " AT ",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


# ── Exchange detection ──────────────────────────────────────────────────────

MCX_KEYWORDS = [
    "GOLD",
    "SILVER",
    "CRUDE",
    "CRUDEOIL",
    "NATURALGAS",
    "COPPER",
    "ZINC",
    "ALUMINIUM",
    "LEAD",
    "NICKEL",
    "GOLDM",
    "SILVERM",
]
INDEX_KEYWORDS = ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "BANKEX"]


def _detect_exchange(symbol: str) -> str:
    s = symbol.upper()
    if any(k in s for k in MCX_KEYWORDS):
        return "MCX"
    if any(k in s for k in INDEX_KEYWORDS):
        return "NFO"
    # Has CE/PE/FUT → options/futures on NFO
    if re.search(r"\d+(CE|PE|FUT)", s):
        return "NFO"
    return "NSE"


def _detect_instrument(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("CE") or "CE " in s:
        return "CE"
    if s.endswith("PE") or "PE " in s:
        return "PE"
    if "FUT" in s:
        return "FUT"
    return "EQ"


# ── Format 1: Compact inline ────────────────────────────────────────────────
# #SENSEX 77400CE buy abv 213 tar 249 291 sl 181

FORMAT1_PATTERN = re.compile(
    r"#?(?P<symbol>[\w]+(?:\s*\d+(?:CE|PE|FUT)?)?)"  # symbol
    r"\s+(?P<action>BUY|SELL|SHORT|LONG)"
    r"(?:\s+(?P<entry_type>ABOVE|BELOW|AT))?"
    r"\s+(?P<entry>[\d.]+)"
    r"(?:.*?TARGET[S]?\s+(?P<targets>[\d.\s]+?))?"  # optional targets
    r"(?:.*?SL\s+(?P<sl>[\d.]+))?",  # optional sl
    re.IGNORECASE | re.DOTALL,
)


def _parse_format1(text: str) -> dict | None:
    norm = _normalize(text)
    m = FORMAT1_PATTERN.search(norm)
    if not m:
        return None

    symbol_raw = m.group("symbol").strip().replace(" ", "")
    action = m.group("action").upper()
    if action in ("SHORT", "LONG"):
        action = "SELL" if action == "SHORT" else "BUY"
    entry_type = (m.group("entry_type") or "LIMIT").upper()
    if entry_type == "AT":
        entry_type = "LIMIT"

    entry = float(m.group("entry")) if m.group("entry") else None
    sl = float(m.group("sl")) if m.group("sl") else None

    targets = []
    if m.group("targets"):
        targets = [float(x) for x in re.findall(r"[\d.]+", m.group("targets"))]
    if entry is None or sl is None:
        return None

    return {
        "action": action,
        "symbol": symbol_raw,
        "instrument_type": _detect_instrument(symbol_raw),
        "expiry": None,
        "exchange": _detect_exchange(symbol_raw),
        "entry_price": entry,
        "entry_type": entry_type,
        "stop_loss": sl,
        "targets": targets,
        "risk_amount": None,
        "quantity": None,
        "trade_type": "INTRADAY",
        "source_channel": None,
        "raw_text": text.strip(),
    }


# ── Format 2: Structured block ──────────────────────────────────────────────
# BUY
# LAURUSLABS APR 1100PE
# ENTRY - 44 / SL - 36 / RISK - 7000


def _parse_format2(text: str) -> dict | None:
    norm = text.upper().strip()

    # Action
    action_m = re.search(r"^(BUY|SELL|SHORT|LONG)", norm, re.MULTILINE)
    if not action_m:
        return None
    action = action_m.group(1)
    if action == "SHORT":
        action = "SELL"
    if action == "LONG":
        action = "BUY"

    # Symbol: line after action
    lines = [l.strip() for l in norm.splitlines() if l.strip()]
    action_idx = next((i for i, l in enumerate(lines) if l.startswith(action)), 0)
    symbol_line = lines[action_idx + 1] if action_idx + 1 < len(lines) else ""

    # Extract expiry from symbol line (e.g. APR, MAY, JUN)
    expiry_m = re.search(
        r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", symbol_line
    )
    expiry = expiry_m.group(1) if expiry_m else None

    # Clean symbol
    symbol = re.sub(
        r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", "", symbol_line
    )
    symbol = re.sub(r"\s+", "", symbol).strip()

    # Entry
    entry_m = re.search(r"ENTRY[\s\-:]+(\d+\.?\d*)", norm)
    entry = float(entry_m.group(1)) if entry_m else None

    # SL
    sl_m = re.search(r"\bSL[\s\-:]+(\d+\.?\d*)", norm)
    sl = float(sl_m.group(1)) if sl_m else None

    # Risk amount
    risk_m = re.search(r"RISK[\s\-:]+(\d+)", norm)
    risk = float(risk_m.group(1)) if risk_m else None

    # Targets
    targets = []
    for t_match in re.finditer(r"TARGET[\d]?[\s\-:]+(\d+\.?\d*)", norm):
        targets.append(float(t_match.group(1)))
    if entry is None or sl is None:
        return None

    return {
        "action": action,
        "symbol": symbol,
        "instrument_type": _detect_instrument(symbol),
        "expiry": expiry,
        "exchange": _detect_exchange(symbol),
        "entry_price": entry,
        "entry_type": "LIMIT",
        "stop_loss": sl,
        "targets": targets,
        "risk_amount": risk,
        "quantity": None,
        "trade_type": "INTRADAY",
        "source_channel": None,
        "raw_text": text.strip(),
    }


def _extract_targets(norm: str) -> list[float]:
    targets = []

    labeled_bundle = re.search(
        r"TARGETS?[\d\s\-:]*((?:\d+(?:\.\d+)?\s+){0,4}\d+(?:\.\d+)?)", norm
    )
    if labeled_bundle:
        targets.extend(
            float(x) for x in re.findall(r"\d+(?:\.\d+)?", labeled_bundle.group(1))
        )

    if not targets:
        for match in re.finditer(r"TARGET[123]?[\s\-:]+(\d+(?:\.\d+)?)", norm):
            targets.append(float(match.group(1)))

    deduped = []
    for value in targets:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _parse_generic_signal(text: str) -> dict | None:
    """Loose fallback for OCR text that doesn't match the strict formats."""
    norm = _normalize(text)
    lines = [line.strip() for line in norm.splitlines() if line.strip()]

    action_m = re.search(r"\b(BUY|SELL|SHORT|LONG)\b", norm)
    if not action_m:
        return None
    action = action_m.group(1)
    if action == "SHORT":
        action = "SELL"
    elif action == "LONG":
        action = "BUY"

    symbol = None
    symbol_m = re.search(
        r"#?((?:BANKNIFTY|MIDCPNIFTY|FINNIFTY|SENSEX|BANKEX|NIFTY|[A-Z]+)\s*(?:[A-Z]{3}\s*)?\d+(?:CE|PE|FUT))",
        norm,
    )
    if symbol_m:
        symbol = symbol_m.group(1).replace(" ", "")
    elif lines:
        for line in lines:
            cleaned = re.sub(
                r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", "", line
            )
            compact = re.sub(r"\s+", "", cleaned)
            if re.search(r"\d+(CE|PE|FUT)", compact):
                symbol = compact
                break

    if not symbol:
        return None

    entry_type = "LIMIT"
    entry_patterns = [
        (r"(?:BUY|SELL)\s+ABOVE[\s\-:]+(\d+(?:\.\d+)?)", "ABOVE"),
        (r"(?:BUY|SELL)\s+BELOW[\s\-:]+(\d+(?:\.\d+)?)", "BELOW"),
        (r"ENTRY[\s\-:]+(\d+(?:\.\d+)?)", "LIMIT"),
        (r"\bAT[\s\-:]+(\d+(?:\.\d+)?)", "LIMIT"),
        (r"\bABOVE[\s\-:]+(\d+(?:\.\d+)?)", "ABOVE"),
        (r"\bBELOW[\s\-:]+(\d+(?:\.\d+)?)", "BELOW"),
    ]

    entry = None
    for pattern, etype in entry_patterns:
        match = re.search(pattern, norm)
        if match:
            entry = float(match.group(1))
            entry_type = etype
            break

    sl_m = re.search(r"(?:\bSL\b|STOPLOSS|STOP)[\s\-:]+(\d+(?:\.\d+)?)", norm)
    sl = float(sl_m.group(1)) if sl_m else None
    targets = _extract_targets(norm)

    if entry is None or sl is None:
        return None

    return {
        "action": action,
        "symbol": symbol,
        "instrument_type": _detect_instrument(symbol),
        "expiry": None,
        "exchange": _detect_exchange(symbol),
        "entry_price": entry,
        "entry_type": entry_type,
        "stop_loss": sl,
        "targets": targets,
        "risk_amount": None,
        "quantity": None,
        "trade_type": "INTRADAY",
        "source_channel": None,
        "raw_text": text.strip(),
    }


# ── Public API ───────────────────────────────────────────────────────────────


class SignalParser:
    def parse_image(self, image_path: str) -> dict | None:
        """Parse a Telegram screenshot → trade signal dict (no API key needed)"""
        # Check if OCR is available
        if not is_ocr_available():
            logger.error(
                "EasyOCR not available - cannot parse images. Install torch, torchvision, easyocr"
            )
            return None

        path = Path(image_path)
        if not path.exists():
            logger.error(f"Image not found: {image_path}")
            return None
        try:
            text = _extract_text_from_image(str(path))
            return self._parse_text_smart(text, source_image=image_path)
        except Exception as e:
            logger.error(f"Image parse error: {e}")
            return None

    def parse_text(self, text: str) -> dict | None:
        """Parse a raw text signal"""
        return self._parse_text_smart(text)

    def _parse_text_smart(self, text: str, source_image: str = None) -> dict | None:
        """Try both formats, return first successful parse"""
        attempts = []

        # Try compact format first (has # or single line with buy/sell + sl)
        if re.search(r"(BUY|SELL|SHORT|LONG).{1,60}(SL|STOP)", text, re.IGNORECASE):
            result = _parse_format1(text)
            if result:
                if source_image:
                    result["signal_image_path"] = source_image
                logger.info(
                    f"Format1 parsed: {result['action']} {result['symbol']} @ {result['entry_price']}"
                )
                return result
            attempts.append("format1")

        # Try structured format
        result = _parse_format2(text)
        if result:
            if source_image:
                result["signal_image_path"] = source_image
            logger.info(
                f"Format2 parsed: {result['action']} {result['symbol']} @ {result['entry_price']}"
            )
            return result
        attempts.append("format2")

        result = _parse_generic_signal(text)
        if result:
            if source_image:
                result["signal_image_path"] = source_image
            logger.info(
                f"Generic parser parsed: {result['action']} {result['symbol']} @ {result['entry_price']}"
            )
            return result
        attempts.append("generic")

        logger.warning(
            f"Could not parse signal from text after attempts {attempts}:\n{text[:300]}"
        )
        return None

    def calculate_quantity(self, signal: dict, lot_size: int = 1) -> int:
        """Calculate qty from risk amount. qty = risk / (entry - sl) rounded to lot"""
        risk = signal.get("risk_amount")
        entry = signal.get("entry_price")
        sl = signal.get("stop_loss")

        if not all([risk, entry, sl]):
            return lot_size

        risk_per_unit = abs(entry - sl)
        if risk_per_unit <= 0:
            return lot_size

        raw_qty = risk / risk_per_unit
        if lot_size > 1:
            lots = max(1, round(raw_qty / lot_size))
            return lots * lot_size
        return max(1, int(raw_qty))


signal_parser = SignalParser()
