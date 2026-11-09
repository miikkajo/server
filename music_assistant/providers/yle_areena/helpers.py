"""Pure helpers for parsing the Yle Areena API responses."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from music_assistant.providers.yle_areena.constants import (
    FEED_URL_TEMPLATE,
    IMAGE_URL_TEMPLATE,
    PODCAST_PAGE_URL_TEMPLATE,
    VIDEO_ITEM_TYPES,
    VIDEO_TITLE_MARKERS,
)

_SERIES_ID_RE = re.compile(r"^1-\d+$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)

_ITEM_URI_PREFIX = "yleareena://items/"
_PACKAGE_URI_PREFIX = "yleareena://packages/"
_CREDENTIALS_MARKER = "window.envVariables"

# content-list endpoints that list episodes unless asked for series explicitly
_EPISODE_LIST_KINDS = frozenset({"latest", "popular", "expiring"})

# Kept as a tuple so the source also parses on Python versions before PEP 758.
_JSON_DECODE_ERRORS = (ValueError, binascii.Error)


@dataclass(slots=True)
class YleCredentials:
    """The frontend API credentials embedded in the Yle Areena podcast page."""

    app_id: str
    app_key: str


@dataclass(slots=True)
class YleCard:
    """A single card as returned by the Yle Areena browse and search endpoints."""

    title: str
    description: str
    card_type: str
    item_id: str
    image_url: str | None = None
    item_type: str | None = None
    highlight: str | None = None
    media_type: str | None = None
    series_id: str | None = None
    duration: int | None = None


@dataclass(slots=True)
class YleCategory:
    """A podcast category from the Areena browse menu."""

    package_id: str
    title: str
    image_url: str | None = None


@dataclass(slots=True)
class YleRow:
    """A single content row of a category package."""

    index: int
    title: str
    source_uri: str
    source: str


def parse_credentials(html: str) -> YleCredentials | None:
    """
    Extract the frontend API credentials from a Yle Areena podcast page.

    :param html: The HTML of the podcast landing page.
    """
    marker_index = html.find(_CREDENTIALS_MARKER)
    if marker_index == -1:
        return None
    start = html.find("{", marker_index)
    if start == -1:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(html[start:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    app_id = data.get("appIdFrontend")
    app_key = data.get("appKeyFrontend")
    if not isinstance(app_id, str) or not isinstance(app_key, str):
        return None
    if not app_id or not app_key:
        return None
    return YleCredentials(app_id=app_id, app_key=app_key)


def is_series_id(value: str) -> bool:
    """
    Return whether the value looks like a Yle Areena series id.

    :param value: The provider item id to validate.
    """
    return bool(_SERIES_ID_RE.match(value))


def series_feed_url(series_id: str, language: str) -> str:
    """
    Build the podcast RSS feed url for a Yle Areena series.

    :param series_id: The Yle Areena series id, e.g. ``1-61894955``.
    :param language: The content language, ``fi`` or ``sv``.
    """
    return f"{FEED_URL_TEMPLATE.format(series_id=series_id)}?language={language}"


def podcast_page_url(series_id: str) -> str:
    """
    Build the public Areena page url for a series.

    :param series_id: The Yle Areena series id.
    """
    return PODCAST_PAGE_URL_TEMPLATE.format(series_id=series_id)


def image_url(image_id: str) -> str:
    """
    Build a square thumbnail url for a Yle image id.

    :param image_id: The image id as returned by the Areena API.
    """
    if image_id.startswith(("http://", "https://")):
        return image_id
    return IMAGE_URL_TEMPLATE.format(image_id=image_id)


def parse_iso_duration(value: Any) -> int | None:
    """
    Convert an ISO 8601 duration such as ``PT2144S`` to whole seconds.

    :param value: The raw duration value.
    """
    if not isinstance(value, str):
        return None
    match = _ISO_DURATION_RE.match(value)
    if match is None:
        return None
    parts = {key: float(raw) for key, raw in match.groupdict().items() if raw is not None}
    total = (
        parts.get("days", 0.0) * 86400
        + parts.get("hours", 0.0) * 3600
        + parts.get("minutes", 0.0) * 60
        + parts.get("seconds", 0.0)
    )
    return int(total)


def parse_category_controls(menu: dict[str, Any]) -> list[YleCategory]:
    """
    Return the podcast categories from a ``browseMenuContent`` response.

    :param menu: The parsed response of the browse menu endpoint.
    """
    data = menu.get("data")
    if not isinstance(data, dict):
        return []
    categories: list[YleCategory] = []
    seen: set[str] = set()
    for block in data.get("podcastBlocks") or []:
        if not isinstance(block, dict) or block.get("type") != "categoryImageLinks":
            continue
        for control in block.get("controls") or []:
            if not isinstance(control, dict) or control.get("type") != "navigator":
                continue
            title = control.get("title")
            destination = control.get("destination")
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(destination, dict):
                continue
            package_id = package_id_from_uri(destination.get("uri"))
            if package_id is None or package_id in seen or is_video_title(title):
                continue
            seen.add(package_id)
            image_id = _image_id_from_card(control)
            categories.append(
                YleCategory(
                    package_id=package_id,
                    title=title.strip(),
                    image_url=image_url(image_id) if image_id else None,
                )
            )
    return categories


def parse_package_rows(package_view: dict[str, Any]) -> list[YleRow]:
    """
    Return the usable content rows of a category package view.

    Rows that list individual episodes instead of series are skipped, as a podcast
    provider cannot turn a one-off radio episode into a subscribable podcast.

    :param package_view: The parsed response of the package view endpoint.
    """
    data = package_view.get("data")
    if not isinstance(data, dict):
        return []
    rows: list[YleRow] = []
    for tab in data.get("tabs") or []:
        if not isinstance(tab, dict):
            continue
        tab_title = tab.get("title")
        for content in tab.get("content") or []:
            if not isinstance(content, dict):
                continue
            source = content.get("source")
            if not isinstance(source, dict):
                continue
            source_uri = source.get("uri")
            if not isinstance(source_uri, str):
                continue
            decoded_source = decode_token_source(source_uri)
            if decoded_source is None or is_episode_only_row(decoded_source):
                continue
            title = content.get("title")
            if not isinstance(title, str) or not title.strip():
                title = tab_title if isinstance(tab_title, str) else ""
            rows.append(
                YleRow(
                    index=len(rows),
                    title=title.strip(),
                    source_uri=source_uri,
                    source=decoded_source,
                )
            )
    return rows


def decode_token_source(source_uri: str) -> str | None:
    """
    Return the upstream source url embedded in a content-list token.

    :param source_uri: The content list url including its JWT ``token`` parameter.
    """
    query = parse_qs(urlparse(source_uri).query)
    token = next(iter(query.get("token") or []), None)
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
    except _JSON_DECODE_ERRORS:
        return None
    if not isinstance(data, dict):
        return None
    source = data.get("source")
    return source if isinstance(source, str) else None


def is_episode_only_row(source: str) -> bool:
    """
    Return whether a content row lists individual episodes instead of series.

    The ``latest``, ``popular`` and ``expiring`` list endpoints return episodes unless
    they are asked for series explicitly; curated recommendation rows always list series.

    :param source: The upstream source url decoded from the row token.
    """
    parsed = urlparse(source)
    kind = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if kind not in _EPISODE_LIST_KINDS:
        return False
    query = parse_qs(parsed.query)
    episodes_as_series = (query.get("episodes_as_series") or ["false"])[0]
    return episodes_as_series.casefold() != "true"


def parse_cards(payload: dict[str, Any]) -> list[YleCard]:
    """
    Return the cards from a content list or search response.

    :param payload: The parsed response of a content list or search endpoint.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    cards: list[YleCard] = []
    for item in data:
        if isinstance(item, dict) and (card := card_from_dict(item)) is not None:
            cards.append(card)
    return cards


def content_page_count(payload: dict[str, Any]) -> int:
    """
    Return the total number of items a content list reports.

    :param payload: The parsed response of a content list endpoint.
    """
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return 0
    count = meta.get("count")
    return count if isinstance(count, int) and count > 0 else 0


def card_from_dict(data: dict[str, Any]) -> YleCard | None:
    """
    Build a card from a single Areena card payload.

    :param data: One card object from a browse, content list or search response.
    """
    pointer = data.get("pointer")
    if not isinstance(pointer, dict):
        return None
    card_type = pointer.get("type")
    if not isinstance(card_type, str):
        return None
    labels = _labels_by_type(data)
    item_id = _item_id_from_uri(pointer.get("uri")) or _label_value(labels.get("itemId"))
    if item_id is None:
        return None
    title = data.get("title")
    description = data.get("description")
    series_id = None
    if card_type == "series":
        series_id = item_id
    elif card_type == "program":
        series_id = _label_value(labels.get("seriesLink"))
    image_id = _image_id_from_card(data)
    duration_label = labels.get("duration") or labels.get("progress")
    return YleCard(
        title=title.strip() if isinstance(title, str) else "",
        description=description if isinstance(description, str) else "",
        card_type=card_type,
        item_id=item_id,
        image_url=image_url(image_id) if image_id else None,
        item_type=_label_value(labels.get("itemtype")),
        highlight=_label_value(labels.get("highlight")),
        media_type=_label_value(labels.get("mediaType")),
        series_id=series_id,
        duration=parse_iso_duration(_label_value(duration_label)),
    )


def is_video_card(card: YleCard) -> bool:
    """
    Return whether a card points at video content without an audio podcast feed.

    :param card: The card to inspect.
    """
    if card.item_type in VIDEO_ITEM_TYPES:
        return True
    if card.highlight and "VIDEO" in card.highlight.upper():
        return True
    return bool(card.media_type and card.media_type.casefold() == "video")


def is_video_title(title: str) -> bool:
    """
    Return whether a category title marks a video-only section.

    :param title: The category title as returned by the API.
    """
    normalized = title.casefold()
    return any(marker in normalized for marker in VIDEO_TITLE_MARKERS)


def package_id_from_uri(uri: Any) -> str | None:
    """
    Return the package id from a ``yleareena://packages/...`` uri.

    :param uri: The destination uri of a navigator control.
    """
    if not isinstance(uri, str) or not uri.startswith(_PACKAGE_URI_PREFIX):
        return None
    package_id = uri.removeprefix(_PACKAGE_URI_PREFIX)
    return package_id or None


def _item_id_from_uri(uri: Any) -> str | None:
    """Return the item id from a ``yleareena://items/...`` uri."""
    if not isinstance(uri, str) or not uri.startswith(_ITEM_URI_PREFIX):
        return None
    item_id = uri.removeprefix(_ITEM_URI_PREFIX)
    return item_id or None


def _labels_by_type(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a card's labels by their type, keeping the first occurrence."""
    labels: dict[str, dict[str, Any]] = {}
    for label in data.get("labels") or []:
        if not isinstance(label, dict):
            continue
        label_type = label.get("type")
        if isinstance(label_type, str):
            labels.setdefault(label_type, label)
    return labels


def _label_value(label: dict[str, Any] | None) -> str | None:
    """Return a label's raw value, falling back to its formatted value."""
    if not label:
        return None
    for key in ("raw", "formatted"):
        value = label.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _image_id_from_card(data: dict[str, Any]) -> str | None:
    """Return the image id of a card or navigator control."""
    image = data.get("image")
    if not isinstance(image, dict):
        return None
    image_id = image.get("id")
    return image_id if isinstance(image_id, str) and image_id else None
