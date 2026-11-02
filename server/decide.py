"""Fast typed decisions: unstructured state in, calibrated probabilities out.

Jev answers a yes/no question with a probability instead of prose, which is what
makes it usable as a gate in front of an expensive model call. It generates no
text, so it never replaces a model — it decides whether one is worth paying for.

Every caller passes a fallback and must work without this module: it is a
third-party network hop, and a decision that cannot be made remotely still has
to be made. Failures return None rather than raising, so a gate can never break
the turn it is gating. State is redacted before it leaves the machine.
"""
import asyncio
import logging
import os

import httpx

import secret_mask
import settings as cfg

log = logging.getLogger("missioncontrol.decide")

BASE_URL = "https://api.typesafe.ai"
MODEL = "jev-latest"
KEY_ENV = "TYPESAFE_API_KEY"
# A gate is only worth having if it is quicker than what it guards. Jev answers
# in 70-500ms; past a couple of seconds the fallback is the better decision.
TIMEOUT = 6.0
ATTEMPTS = 2


def _conf() -> dict:
    try:
        return cfg.load().get("decide") or {}
    except Exception:
        return {}


def _key() -> str:
    return os.getenv(KEY_ENV) or cfg.env_get(KEY_ENV) or ""


def configured() -> bool:
    """True when remote decisions are available and switched on."""
    return bool(_key()) and bool(_conf().get("enabled", True))


def _clean(state):
    """Redact before the state leaves the machine, at every depth."""
    if isinstance(state, str):
        return secret_mask.redact(state)
    if isinstance(state, dict):
        return {k: _clean(v) for k, v in state.items()}
    if isinstance(state, list):
        return [_clean(v) for v in state]
    return state


async def _post(payload: dict) -> dict | None:
    base = (_conf().get("base_url") or BASE_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    delay = 0.4
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=3.0)) as client:
        for attempt in range(ATTEMPTS):
            try:
                r = await client.post(f"{base}/v1/systemone", json=payload, headers=headers)
                if r.status_code == 200:
                    return r.json()
                # Retrying a 422 just spends the latency budget again: the request
                # is wrong, not unlucky.
                if r.status_code in (429, 500, 502, 503, 529) and attempt < ATTEMPTS - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                log.info("decide HTTP %s: %s", r.status_code, r.text[:200])
                return None
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < ATTEMPTS - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                log.info("decide unreachable: %s", exc)
                return None
    return None


async def nouls(state, questions: dict[str, dict]) -> dict[str, float] | None:
    """Ask several yes/no questions about one state in a single round trip.

    `questions` maps a name to {"instructions": str, "criteria": {"true", "false"}}.
    Returns {name: probability 0-1}, or None when no remote answer was obtained —
    None means "decide this yourself", never "no".
    """
    if not configured() or not questions:
        return None
    payload = {"state": _clean(state), "model": _conf().get("model") or MODEL,
               "questions": {
                   name: {"type": "noul", "instructions": q["instructions"],
                          **({"criteria": q["criteria"]} if q.get("criteria") else {})}
                   for name, q in questions.items()}}
    data = await _post(payload)
    if not data:
        return None
    answers = data.get("answers") or {}
    out = {}
    for name in questions:
        value = (answers.get(name) or {}).get("noul")
        if isinstance(value, (int, float)):
            out[name] = float(value)
    if not out:
        log.info("decide returned no usable answers for %s", sorted(questions))
        return None
    return out


async def noul(state, instructions: str, criteria: dict | None = None) -> float | None:
    """One yes/no question. Returns its probability, or None when unavailable."""
    got = await nouls(state, {"q": {"instructions": instructions, "criteria": criteria}})
    return got.get("q") if got else None


async def choice(state, instructions: str, criteria: dict) -> tuple[str, float] | None:
    """Pick one of `criteria` (name -> what it means) for this state.

    Returns (name, its probability) so the caller can hold out for a margin
    rather than taking the winner of a three-way tie, or None when unavailable.
    """
    if not configured() or not criteria:
        return None
    data = await _post({"state": _clean(state), "model": _conf().get("model") or MODEL,
                        "questions": {"q": {"type": "choice", "instructions": instructions,
                                            "criteria": criteria}}})
    answer = (data or {}).get("answers", {}).get("q") or {}
    picked = answer.get("choice")
    if picked not in criteria:
        if data:
            log.info("decide returned unusable choice %r", picked)
        return None
    probability = (answer.get("probabilities") or {}).get(picked)
    if not isinstance(probability, (int, float)):
        probability = answer.get("confidence")
    return (picked, float(probability)) if isinstance(probability, (int, float)) else None
