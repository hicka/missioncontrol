"""Text-to-image for the team (MiniMax image-01), saved into uploads so it
posts into chat and mirrors to phones like any other attachment."""
import asyncio
import hashlib

import settings as cfg
import uploads

MINIMAX_HOST = "https://api.minimax.io"
ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}


def available() -> bool:
    return bool(cfg.env_get("MINIMAX_API_KEY"))


async def generate(prompt: str, aspect: str = "1:1") -> tuple[dict | None, str | None]:
    """(attachment, error). The attachment matches the shape chat and the
    phone channels already render."""
    prompt = " ".join((prompt or "").split())[:1500]
    if not prompt:
        return None, "empty prompt"
    key = cfg.env_get("MINIMAX_API_KEY")
    if not key:
        return None, "no MINIMAX_API_KEY set; image generation needs the MiniMax key"
    if aspect not in ASPECTS:
        aspect = "1:1"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{MINIMAX_HOST}/v1/image_generation",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "image-01", "prompt": prompt, "n": 1,
                      "response_format": "url", "aspect_ratio": aspect})
            d = r.json()
            if (d.get("base_resp") or {}).get("status_code") != 0:
                return None, (d.get("base_resp") or {}).get("status_msg") or "image generation failed"
            urls = (d.get("data") or {}).get("image_urls") or []
            if not urls:
                return None, "no image returned"
            img = await client.get(urls[0], timeout=60.0)
            data = img.content
    except Exception as exc:
        return None, f"{exc.__class__.__name__}"
    name = "image-" + hashlib.sha1(prompt.encode()).hexdigest()[:8] + ".jpg"
    dest = await asyncio.to_thread(uploads.save, data, name)
    return {"name": dest.name, "path": str(dest), "url": f"/uploads/{dest.name}",
            "mime": "image/jpeg", "kind": "image", "size": len(data),
            "desc": prompt[:200]}, None
