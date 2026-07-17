from __future__ import annotations

import re


def _normalize_key(value: str) -> str:
    return re.sub(r"[\s_]+", "-", value.strip().lower())


SUPPORTED_EDGE_MODELS = (
    "edge500",
    "edge5X0",
    "edge510",
    "edge510lte",
    "edge840",
    "edge1000",
    "edge2000",
    "edge3400",
    "edge3800",
    "edge3810",
    "virtual",
    "edge540",
    "edge520",
    "edge520-v",
    "edge610",
    "edge610lte",
    "edge620",
    "aliyun",
    "edge640",
    "edge680",
    "edge6X0",
    "edge3X00",
    "edge3X10",
    "edge710",
    "edge720",
    "edge740",
    "edge7X0",
    "edge4100",
    "edge5100",
)

_CANONICAL_MODELS = {_normalize_key(model): model for model in SUPPORTED_EDGE_MODELS}
_RAW_VELOCLOUD_MODEL_MAP = {
    "3400": ("edge3X00", "3400"),
    "3800": ("edge3X00", "3800"),
    "3810": ("edge3X10", "3810"),
    "500": ("edge500", "500"),
    "510": ("edge510", "510"),
    "510lte": ("edge510lte", "510"),
    "520": ("edge5X0", "520"),
    "520-v": ("edge520-v", "520-v"),
    "540": ("edge5X0", "540"),
    "610": ("edge6X0", "610"),
    "610lte": ("edge610lte", "610"),
    "620": ("edge6X0", "620"),
    "640": ("edge6X0", "640"),
    "680": ("edge6X0", "680"),
    "710": ("edge710", "710"),
    "7105g": ("edge710", "7105g"),
    "720": ("edge7X0", "720"),
    "740": ("edge7X0", "740"),
    "840": ("edge840", "840"),
    "1000": ("edge1000", "1000"),
    "2000": ("edge2000", "2000"),
    "4100": ("edge4100", "4100"),
    "5100": ("edge5100", "5100"),
    "virtual": ("virtual", None),
    "aliyun": ("aliyun", None),
}


def normalize_edge_model(
    model: str | None,
    model_suffix: str | None = None,
) -> tuple[str | None, str | None]:
    if not model:
        return None, model_suffix

    canonical = _CANONICAL_MODELS.get(_normalize_key(model))
    if canonical:
        return canonical, model_suffix or extract_edge_model_suffix(model)

    lowered = _normalize_key(model)
    if lowered.startswith("velocloud-"):
        raw_model = lowered.removeprefix("velocloud-")
        normalized = _RAW_VELOCLOUD_MODEL_MAP.get(raw_model)
        if normalized:
            canonical_model, default_suffix = normalized
            return canonical_model, model_suffix or default_suffix or extract_edge_model_suffix(model)

    return model, model_suffix or extract_edge_model_suffix(model)


def extract_edge_model_suffix(model: str | None) -> str | None:
    if not model:
        return None
    match = re.search(r"(\d+(?:[a-z0-9-]*[a-z0-9])?)", model.lower())
    return match.group(1) if match else None
