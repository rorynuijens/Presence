"""
slides/image_gen.py — Pure Gemini image generation library (no GTK dependency).

Provides async functions and style constants for generating slide images via
the Gemini API. Imported by ai_import_dialog.py and ai_image_dialog.py.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import uuid

log = logging.getLogger(__name__)

IMAGE_STYLE_LABELS: list[str] = [
    "Photorealistic",
    "Pencil drawing",
    "Pop art",
    "Black & white photo",
    "Cartoon",
    "Ligne Claire",
    "Realistic Anime",
    "Cyberpunk Neon",
]

IMAGE_STYLE_PROMPTS: dict[str, str] = {
    "Photorealistic":
        "{scene}. Photorealistic photograph, professional lighting, wide format 16:9, no text or logos.",
    "Pencil drawing":
        "Pencil sketch of {scene}. Black and white, hand-drawn, fine cross-hatching, no colour, wide format 16:9, no text or logos.",
    "Pop art":
        "Pop art illustration of {scene}. Flat colours, bold black outlines, Ben-Day dot pattern, Roy Lichtenstein style, wide format 16:9, no text or logos.",
    "Black & white photo":
        "Black and white photograph of {scene}. High contrast, dramatic shadows, wide format 16:9, no text or logos.",
    "Cartoon":
        "Cartoon illustration of {scene}. Flat colours, bold black outlines, simple shapes, bright colours, wide format 16:9, no text or logos.",
    "Ligne Claire":
        "Ligne claire illustration of {scene}. Clean uniform black outlines, flat pastel colours, no shading, Hergé / Tintin style, wide format 16:9, no text or logos.",
    "Realistic Anime":
        "Realistic anime illustration of {scene}. Detailed semi-realistic character art, soft cel shading, vibrant colours, cinematic composition, Studio Ghibli / Makoto Shinkai style, wide format 16:9, no text or logos.",
    "Cyberpunk Neon":
        "Cyberpunk neon illustration of {scene}. Dark rain-soaked cityscape, vivid neon pink and cyan lighting, volumetric fog, reflective wet surfaces, Blade Runner aesthetic, wide format 16:9, no text or logos.",
}

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-image"


def build_gemini_prompt(scene: str, style: str = "Photorealistic") -> str:
    template = IMAGE_STYLE_PROMPTS.get(style, IMAGE_STYLE_PROMPTS["Photorealistic"])
    return template.format(scene=scene)


def slide_headline(slide_text: str) -> str:
    """Return the first # heading text from a slide's raw Markdown."""
    for line in slide_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def slide_image_scene(slide_text: str) -> str:
    """Extract the scene description from the first gemini image alt text in a slide."""
    m = re.search(
        r'!\[([^|\]]+)\|[^\]]*\]\(assets/gemini_\d+\.jpg\)', slide_text
    )
    return m.group(1).strip() if m else ""


def scene_hint_from_slide(slide_text: str) -> str:
    """
    Build a scene-description hint from a slide's raw Markdown.

    Combines the first heading and (when a ^^^ section is present) the first
    one or two sentences of the speaker notes.  The result is a concrete
    starting point the user can refine into a visual scene for Gemini.
    """
    headline = slide_headline(slide_text)

    notes = ""
    if "^^^" in slide_text:
        raw = slide_text.split("^^^", 1)[1].strip()
        # Collapse newlines and extra whitespace into a single block
        raw = re.sub(r'\s+', ' ', raw)
        # Take the first two sentences at most
        parts = re.split(r'(?<=[.!?])\s+', raw, maxsplit=2)
        notes = ' '.join(parts[:2]).strip()

    if headline and notes:
        return f"{headline}. {notes}"
    return headline or notes


async def _call_gemini(
    client, types,
    scene: str, output_path: str,
    image_style: str, gemini_model: str,
) -> str:
    """Core Gemini call: generate one image, write it to output_path, return path."""
    prompt = build_gemini_prompt(scene, image_style)
    log.debug("Gemini prompt → %s: %s", output_path, prompt)

    response = await client.aio.models.generate_content(
        model=gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
    )

    if not response.candidates:
        raise ValueError(f"No candidates in Gemini response (scene: {scene!r})")

    for part in (response.candidates[0].content.parts or []):
        if not part.inline_data:
            continue
        mime = part.inline_data.mime_type or ""
        if not mime.startswith("image/"):
            continue
        data = part.inline_data.data
        if isinstance(data, str):
            data = base64.b64decode(data)
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        log.debug("Written %d bytes → %s", len(data), output_path)
        return output_path

    raise ValueError(f"No image part in Gemini response for scene: {scene!r}")


async def generate_slide_image(
    client, types,
    scene: str, slide_index: int, output_dir: str,
    image_style: str = "Photorealistic",
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    """Generate an image for a numbered slide, saved as gemini_<index>.jpg."""
    output_path = os.path.join(output_dir, f"gemini_{slide_index}.jpg")
    return await _call_gemini(client, types, scene, output_path, image_style, gemini_model)


async def generate_single_image(
    gemini_key: str, scene: str, output_dir: str,
    image_style: str = "Photorealistic",
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    """
    Generate a single image with a UUID-based filename.

    Returns the full path to the saved JPEG.
    Used by the Insert Image → Generate with AI dialog.
    """
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        raise ImportError(
            f"google-genai not installed or failed to import: {exc}"
        ) from exc

    client = genai.Client(api_key=gemini_key)
    try:
        filename = f"gemini_{uuid.uuid4().hex[:12]}.jpg"
        output_path = os.path.join(output_dir, filename)
        return await _call_gemini(client, types, scene, output_path, image_style, gemini_model)
    finally:
        if hasattr(client, "aclose"):
            await client.aclose()


async def _throttled_image(
    semaphore: asyncio.Semaphore, client, types,
    scene: str, slide_index: int, output_dir: str,
    image_style: str = "Photorealistic",
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    async with semaphore:
        log.debug("Slide %d — semaphore acquired", slide_index)
        try:
            return await asyncio.wait_for(
                generate_slide_image(
                    client, types, scene, slide_index,
                    output_dir, image_style, gemini_model,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Slide {slide_index} timed out after 120 s")


async def generate_all_images(
    markdown: str | None,
    gemini_key: str,
    output_dir: str,
    override_tasks: list[tuple[str, int]] | None = None,
    image_style: str = "Photorealistic",
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> None:
    """
    Generate images for all slides in a Markdown deck (max 3 concurrent).

    If override_tasks is supplied, only generate those (scene, index) pairs;
    otherwise the markdown is scanned for scene descriptions or headlines.
    """
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        log.warning(
            "google-genai not installed or failed to import (%s) — skipping image generation.", exc
        )
        return

    client = genai.Client(api_key=gemini_key)
    semaphore = asyncio.Semaphore(3)
    try:
        if override_tasks is not None:
            work = override_tasks
            log.debug("Gemini: retrying %d missing images", len(work))
        else:
            slides = (markdown or "").split("---")
            log.debug("Gemini: %d slides in Markdown", len(slides))
            work = []
            for i, slide in enumerate(slides):
                scene = slide_image_scene(slide) or slide_headline(slide)
                log.debug("Slide %d — scene: %r", i, scene)
                if scene:
                    work.append((scene, i))

        tasks = [
            _throttled_image(
                semaphore, client, types, scene, idx,
                output_dir, image_style, gemini_model,
            )
            for scene, idx in work
        ]
        log.debug("Gemini: launching %d tasks (max 3 concurrent)", len(tasks))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                log.warning("Image gen failed for task %d: %s", i, result)
            else:
                log.debug("Generated: %s", result)
    finally:
        if hasattr(client, "aclose"):
            await client.aclose()
