"""
ReqIF Image Descriptor — LLM Vision for Hardware Diagrams & Formulas
=====================================================================

Uses the GPT4IFX vision model to extract textual descriptions from:
  - Block diagrams (SVG rendered to PNG for the API, or PNG directly)
  - Timing diagrams
  - Formula images (MathML rendered as PNG)
  - Register bitfield diagrams (HTML-table-based, optional)

Descriptions are cached in a JSON file to avoid re-processing images
that haven't changed. Uses the same ChatOpenAI + token_manager patterns
as the existing pdf_parser.

Usage::

    from reqif_image_descriptor import ReqIFImageDescriptor

    descriptor = ReqIFImageDescriptor(cache_dir=Path("temp/reqif_cache"))
    description = descriptor.describe_image(image_bytes, image_type="diagram")
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import httpx
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DEFAULT_MODEL = "gpt-5.2"
_API_TIMEOUT = 300       # 5 minutes per API call
_MAX_RETRIES = 3
_MAX_WORKERS = 20        # Parallel image description threads
_RPM_LIMIT = 28          # Stay under GPT4IFX 30 RPM gateway limit

_CA_BUNDLE_PATH = Path(__file__).resolve().parents[2] / "ca-bundle.crt"


# ---------------------------------------------------------------------------
# Rate Limiter (token bucket — thread-safe)
# ---------------------------------------------------------------------------
class _RateLimiter:
    """Simple token-bucket rate limiter for API calls."""

    def __init__(self, rpm: int):
        self._interval = 60.0 / rpm  # seconds between requests
        self._lock = threading.Lock()
        self._last_call = 0.0

    def acquire(self):
        """Block until we can make the next request within the rate limit."""
        with self._lock:
            now = time.monotonic()
            wait = self._last_call + self._interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DIAGRAM_PROMPT = """\
This is a hardware block diagram or timing diagram from an AURIX \
microcontroller user manual.

Describe this diagram in detail:
1. What is the diagram showing overall? (purpose/context)
2. What are the main components/blocks shown?
3. How are the components connected? What signals flow between them?
4. What is the functional logic? (step-by-step data/signal flow)
5. Any important control signals, multiplexers, or modes shown?

Be precise and technical. Use the exact signal/register names visible in \
the diagram. Format as structured text (not markdown headings)."""

_FORMULA_PROMPT = """\
This is a mathematical formula or equation rendered from MathML in an \
AURIX microcontroller user manual. The image shows a small formula \
(fraction, subscript, superscript, etc.).

Extract the exact mathematical expression shown in this image.
- Use LaTeX-style notation for fractions: f_GPT / 4 or f_{GPT} / 2
- Include subscripts: f_GPT, f_SPB, T3, BPS1, etc.
- If it's a frequency/timing formula, note the relationship
- Common patterns: clock dividers like f_GPT/2, f_GPT/4, f_SPB/2^n

Format: just the formula text on a single line. \
Example: "f_GPT / 4" or "f_SPB / 2^(BPS1+1)"
Do NOT say the image is blank or unreadable — it contains a formula."""

_REGISTER_PROMPT = """\
This is a register bitfield layout diagram from an AURIX microcontroller \
user manual.

Describe the register structure:
1. Register name and total width (bits)
2. For each field: name, bit positions, access type (rw/r/w)
3. Reserved/unused fields

Format as a structured list."""


# ---------------------------------------------------------------------------
# Image Description Cache
# ---------------------------------------------------------------------------

class _ImageCache:
    """JSON-file-backed cache keyed by image content hash."""

    def __init__(self, cache_file: Path):
        self._file = cache_file
        self._data: dict[str, str] = {}
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                with open(self._file, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
                logger.info("Loaded %d cached image descriptions", len(self._data))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get(self, content_hash: str) -> Optional[str]:
        return self._data.get(content_hash)

    def put(self, content_hash: str, description: str):
        self._data[content_hash] = description

    def __len__(self):
        return len(self._data)


# ---------------------------------------------------------------------------
# Main Descriptor Class
# ---------------------------------------------------------------------------

class ReqIFImageDescriptor:
    """Describes hardware images using LLM vision."""

    def __init__(
        self,
        cache_dir: Path | str = Path("temp/reqif_image_cache"),
        model: str = _DEFAULT_MODEL,
        max_workers: int = _MAX_WORKERS,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.model = model
        self.max_workers = max_workers
        self._api_key = api_key
        self._base_url = base_url or "https://gpt4ifx.icp.infineon.com"
        self._client: Optional[ChatOpenAI] = None
        self._cache = _ImageCache(self.cache_dir / "descriptions.json")
        self._stats = {"cached": 0, "described": 0, "failed": 0}
        self._rate_limiter = _RateLimiter(_RPM_LIMIT)

    def _get_client(self) -> ChatOpenAI:
        """Lazily build the LLM client (auto-refreshes token)."""
        if self._client is None:
            api_key = self._api_key
            if not api_key:
                # Import token_manager from the same repo
                import sys
                code_dir = Path(__file__).resolve().parents[1]
                if str(code_dir) not in sys.path:
                    sys.path.insert(0, str(code_dir))
                from token_manager import ensure_valid_token
                api_key = ensure_valid_token()

            ca_bundle = str(_CA_BUNDLE_PATH) if _CA_BUNDLE_PATH.exists() else None
            verify = ca_bundle if ca_bundle else False

            base_url = self._base_url.rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]

            http_client = httpx.Client(verify=verify, timeout=httpx.Timeout(_API_TIMEOUT))
            self._client = ChatOpenAI(
                api_key=api_key,
                base_url=base_url,
                model=self.model,
                max_completion_tokens=4000,
                temperature=0,
                http_client=http_client,
                request_timeout=_API_TIMEOUT,
            )
        return self._client

    @staticmethod
    def _hash_image(image_bytes: bytes) -> str:
        """SHA-256 hash of image content for cache key."""
        return hashlib.sha256(image_bytes).hexdigest()[:16]

    def _get_prompt(self, image_type: str) -> str:
        """Select the appropriate prompt based on image type."""
        if image_type == "formula":
            return _FORMULA_PROMPT
        elif image_type == "register_bitfield":
            return _REGISTER_PROMPT
        else:
            return _DIAGRAM_PROMPT

    def describe_image(
        self,
        image_bytes: bytes,
        image_type: str = "diagram",
        image_path: str = "",
    ) -> str:
        """Describe a single image using LLM vision.

        Args:
            image_bytes: Raw PNG/SVG bytes (SVG will be noted but sent as-is)
            image_type: One of "diagram", "formula", "register_bitfield", "other"
            image_path: Original path (for logging)

        Returns:
            Textual description of the image content.
        """
        content_hash = self._hash_image(image_bytes)

        # Check cache
        cached = self._cache.get(content_hash)
        if cached:
            self._stats["cached"] += 1
            return cached

        # SVG images need to be converted to PNG for vision API
        # For now, if it's SVG, we note it but can't send directly
        if image_path.lower().endswith('.svg'):
            try:
                description = self._describe_svg(image_bytes, image_type)
            except Exception as e:
                logger.warning("SVG description failed for %s: %s", image_path, e)
                description = f"[SVG diagram: {image_path} — LLM description not available for SVG format]"
                self._stats["failed"] += 1
                return description
        else:
            # PNG — send directly
            description = self._call_vision(image_bytes, image_type, image_path)

        if description:
            self._cache.put(content_hash, description)
            self._stats["described"] += 1
        else:
            self._stats["failed"] += 1
            description = f"[Image description failed: {image_path}]"

        return description

    def _describe_svg(self, svg_bytes: bytes, image_type: str) -> str:
        """Handle SVG images.

        Strategy: try to convert SVG → PNG using cairosvg (if available),
        then send the PNG to vision API. If cairosvg isn't available,
        try sending SVG as text context.
        """
        try:
            import cairosvg
            png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=1200)
            return self._call_vision(png_bytes, image_type, "converted.png")
        except ImportError:
            # No cairosvg — extract text labels from SVG XML as fallback
            svg_text = svg_bytes.decode('utf-8', errors='replace')
            # Extract text elements from SVG
            import re
            texts = re.findall(r'>([^<]{2,})<', svg_text)
            meaningful = [t.strip() for t in texts if t.strip() and len(t.strip()) > 1]
            if meaningful:
                labels = ", ".join(meaningful[:50])
                return f"[SVG diagram with labels: {labels}]"
            return "[SVG diagram — text extraction not available]"

    @staticmethod
    def _preprocess_formula_image(image_bytes: bytes) -> bytes:
        """Upscale tiny formula PNGs so the LLM can read them.

        ReqIF MathML formulas are rendered as tiny PNGs (often 30x25 pixels).
        The LLM cannot read these at original size. This method:
        1. Converts RGBA → RGB with white background (transparent bg looks blank)
        2. Upscales 6x using LANCZOS for smooth edges
        3. Re-encodes as PNG
        """
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))

        # Convert RGBA to RGB with white background
        if img.mode == 'RGBA':
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # Only upscale if image is small (< 200px in either dimension)
        if img.size[0] < 200 or img.size[1] < 200:
            scale = max(6, 200 // min(img.size[0], img.size[1]))
            new_size = (img.size[0] * scale, img.size[1] * scale)
            img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    def _call_vision(
        self,
        image_bytes: bytes,
        image_type: str,
        image_path: str,
        retry: int = 0,
    ) -> str:
        """Send image to LLM vision API and return description."""
        # Preprocess formula images (tiny PNGs that LLM can't read at original size)
        if image_type == "formula":
            image_bytes = self._preprocess_formula_image(image_bytes)

        prompt = self._get_prompt(image_type)
        b64 = base64.b64encode(image_bytes).decode()

        # Determine MIME type
        if image_path.lower().endswith('.png') or not image_path:
            mime = "image/png"
        elif image_path.lower().endswith('.jpg') or image_path.lower().endswith('.jpeg'):
            mime = "image/jpeg"
        else:
            mime = "image/png"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ]

        try:
            self._rate_limiter.acquire()
            client = self._get_client()
            resp = client.invoke(messages)
            return getattr(resp, "content", "")
        except Exception as exc:
            if retry < _MAX_RETRIES - 1:
                wait_secs = 10 * (retry + 1)  # Escalating backoff: 10s, 20s
                logger.warning(
                    "Image %s attempt %d failed (%s). Retrying in %ds…",
                    image_path, retry + 1, exc, wait_secs,
                )
                time.sleep(wait_secs)
                # Force client refresh on auth errors
                if "401" in str(exc) or "auth" in str(exc).lower():
                    self._client = None
                return self._call_vision(image_bytes, image_type, image_path, retry + 1)
            logger.error("Image %s failed after %d retries: %s", image_path, _MAX_RETRIES, exc)
            return ""

    def describe_batch(
        self,
        images: list[tuple[bytes, str, str]],
        progress_callback: Optional[callable] = None,
    ) -> dict[str, str]:
        """Describe multiple images in parallel.

        Args:
            images: List of (image_bytes, image_type, image_path) tuples
            progress_callback: Optional fn(completed, total) for progress

        Returns:
            Dict mapping image_path → description
        """
        results: dict[str, str] = {}
        total = len(images)

        # Check which are already cached
        to_process = []
        for img_bytes, img_type, img_path in images:
            content_hash = self._hash_image(img_bytes)
            cached = self._cache.get(content_hash)
            if cached:
                results[img_path] = cached
                self._stats["cached"] += 1
            else:
                to_process.append((img_bytes, img_type, img_path))

        logger.info("Image batch: %d total, %d cached, %d to process",
                    total, total - len(to_process), len(to_process))

        if not to_process:
            return results

        # Process remaining in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for img_bytes, img_type, img_path in to_process:
                future = executor.submit(self.describe_image, img_bytes, img_type, img_path)
                futures[future] = img_path

            completed = total - len(to_process)
            for future in as_completed(futures):
                img_path = futures[future]
                try:
                    description = future.result()
                    results[img_path] = description
                except Exception as exc:
                    logger.error("Failed to describe %s: %s", img_path, exc)
                    results[img_path] = f"[Error: {exc}]"
                    self._stats["failed"] += 1

                completed += 1
                if progress_callback:
                    progress_callback(completed, total)

        # Save cache after batch
        self._cache.save()
        return results

    def save_cache(self):
        """Persist the description cache to disk."""
        self._cache.save()

    @property
    def stats(self) -> dict:
        return dict(self._stats)
