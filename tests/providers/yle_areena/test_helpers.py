"""Tests for the Yle Areena payload helpers."""

from __future__ import annotations

import base64
import json
from typing import Any

from music_assistant.providers.yle_areena.helpers import (
    card_from_dict,
    content_page_count,
    decode_token_source,
    image_url,
    is_episode_only_row,
    is_series_id,
    is_video_card,
    is_video_title,
    parse_cards,
    parse_category_controls,
    parse_credentials,
    parse_iso_duration,
    parse_package_rows,
    podcast_page_url,
    series_feed_url,
)


def _token(payload: dict[str, Any]) -> str:
    """Build a JWT-shaped token carrying the given payload."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _list_uri(source: str) -> str:
    """Build a tokenized content list uri for the given upstream source."""
    return (
        "https://areena.api.yle.fi/v1/ui/content/list"
        f"?client=yle-areena-web&language=fi&v=10&token={_token({'source': source})}"
    )


def _series_card(item_id: str = "1-61894955") -> dict[str, Any]:
    """Build a series card as the browse and search endpoints return one."""
    return {
        "title": "Uutispodcast",
        "description": "Otsikoiden takana joka arkipäivä",
        "pointer": {"type": "series", "uri": f"yleareena://items/{item_id}"},
        "image": {"id": "13-1-61894955-1793710657832", "version": "1793710665"},
        "labels": [
            {"type": "itemtype", "raw": "https://schema.org/RadioSeries"},
            {"type": "itemId", "raw": item_id},
        ],
        "presentation": "seriesCard",
        "type": "card",
    }


def test_parse_credentials() -> None:
    """The frontend credentials are read from the embedded env variables."""
    html = (
        '<script>window.envVariables = {"appIdFrontend":"areena-web-items",'
        '"appKeyFrontend":"secret-key","other":"value"};</script>'
    )
    credentials = parse_credentials(html)
    assert credentials is not None
    assert credentials.app_id == "areena-web-items"
    assert credentials.app_key == "secret-key"


def test_parse_credentials_missing_or_invalid() -> None:
    """A page without usable credentials yields None."""
    assert parse_credentials("<html></html>") is None
    assert parse_credentials("window.envVariables = {not json}") is None
    assert parse_credentials('window.envVariables = {"appIdFrontend":"only-id"};') is None


def test_feed_and_page_urls() -> None:
    """The series id is turned into the public feed and page urls."""
    assert series_feed_url("1-61894955", "fi") == (
        "https://feeds.yle.fi/areena/v1/series/1-61894955.rss?language=fi"
    )
    assert series_feed_url("1-61894955", "sv") == (
        "https://feeds.yle.fi/areena/v1/series/1-61894955.rss?language=sv"
    )
    assert podcast_page_url("1-61894955") == "https://areena.yle.fi/1-61894955"


def test_image_url() -> None:
    """Image ids are expanded to square thumbnails, absolute urls are kept."""
    assert image_url("13-1-61894955") == (
        "https://img.img-cdn.yle.fi/crop_fill,ar_1:1,w_600/13-1-61894955"
    )
    assert image_url("https://example.com/image.jpg") == "https://example.com/image.jpg"


def test_parse_iso_duration() -> None:
    """ISO 8601 durations are converted to whole seconds."""
    assert parse_iso_duration("PT2144S") == 2144
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("P1DT2H") == 93600
    assert parse_iso_duration("not-a-duration") is None
    assert parse_iso_duration(None) is None


def test_is_series_id() -> None:
    """Only Areena series ids are accepted as podcast ids."""
    assert is_series_id("1-61894955")
    assert not is_series_id("1-78588550 ")
    assert not is_series_id("57-DDRw1lBwg")
    assert not is_series_id("")


def test_decode_token_source() -> None:
    """The upstream source is read from the content list token."""
    source = "https://programs.api.yle.fi/v3/schema/v3/packages/30-150/latest"
    assert decode_token_source(_list_uri(source)) == source
    assert decode_token_source("https://example.com/list?token=broken") is None
    assert decode_token_source("https://example.com/list") is None


def test_is_episode_only_row() -> None:
    """Episode list rows are detected unless they are asked for series explicitly."""
    assert is_episode_only_row(
        "https://programs.api.yle.fi/v3/schema/v3/packages/30-5629/latest"
        "?grouping=ondemand.publication.date"
    )
    assert is_episode_only_row(
        "https://programs.api.yle.fi/v3/schema/v3/packages/30-149/popular?episodes_as_series=false"
    )
    assert is_episode_only_row(
        "https://programs.api.yle.fi/v3/schema/v3/packages/30-149/expiring?episodes_as_series=false"
    )
    assert not is_episode_only_row(
        "https://programs.api.yle.fi/v3/schema/v3/packages/30-5629/latest?episodes_as_series=true"
    )
    assert not is_episode_only_row(
        "https://programs.api.yle.fi/v3/schema/v3/packages/30-149/popular?episodes_as_series=true"
    )
    assert not is_episode_only_row(
        "https://programs.api.yle.fi/v3/schema/v3/packages/30-1635/extended-recommendations"
    )


def test_parse_category_controls() -> None:
    """Only podcast navigator categories are returned, video sections are skipped."""
    menu = {
        "data": {
            "podcastBlocks": [
                {
                    "type": "categoryImageLinks",
                    "controls": [
                        {
                            "type": "navigator",
                            "title": "Lapset",
                            "destination": {"uri": "yleareena://packages/57-DDRw1lBwg"},
                            "image": {"id": "13-57-1740555779251"},
                        },
                        {
                            "type": "navigator",
                            "title": "Videopodcastit",
                            "destination": {"uri": "yleareena://packages/30-5145"},
                        },
                    ],
                },
                {
                    "type": "textLinks",
                    "controls": [
                        {
                            "type": "navigator",
                            "title": "Löydä lisää",
                            "destination": {"uri": "yleareena://packages/30-5193"},
                        }
                    ],
                },
                {
                    "type": "categoryImageLinks",
                    "controls": [
                        {
                            "type": "activator",
                            "title": "Yle Arenan på svenska",
                            "functionality": {"id": "switch-language"},
                        }
                    ],
                },
            ]
        }
    }
    categories = parse_category_controls(menu)
    assert [category.package_id for category in categories] == ["57-DDRw1lBwg"]
    assert categories[0].title == "Lapset"
    assert categories[0].image_url is not None


def test_parse_package_rows() -> None:
    """Episode-only rows are dropped and unnamed rows fall back to the tab title."""
    view = {
        "data": {
            "title": "Uutiset ja ajankohtaiset",
            "tabs": [
                {
                    "title": "Suositellut",
                    "content": [
                        {
                            "title": "Ajankohtaista",
                            "source": {
                                "uri": _list_uri(
                                    "https://programs.api.yle.fi/v3/schema/v3/packages/30-150"
                                    "/latest"
                                )
                            },
                        },
                        {
                            "title": "Politiikkaa ja taloutta",
                            "source": {
                                "uri": _list_uri(
                                    "https://programs.api.yle.fi/v3/schema/v3/packages/30-1635"
                                    "/extended-recommendations"
                                )
                            },
                        },
                    ],
                },
                {
                    "title": "Uusimmat",
                    "content": [
                        {
                            "source": {
                                "uri": _list_uri(
                                    "https://programs.api.yle.fi/v3/schema/v3/packages/30-5629"
                                    "/latest?grouping=ondemand.publication.date"
                                )
                            }
                        }
                    ],
                },
            ],
        }
    }
    rows = parse_package_rows(view)
    # the plain /latest row lists individual episodes, so only the curated row remains
    assert [row.index for row in rows] == [0]
    assert rows[0].title == "Politiikkaa ja taloutta"
    assert "extended-recommendations" in rows[0].source


def test_parse_package_rows_unnamed_row_uses_tab_title() -> None:
    """A row without its own title inherits the tab title."""
    view = {
        "data": {
            "tabs": [
                {
                    "title": "Suosituimmat",
                    "content": [
                        {
                            "source": {
                                "uri": _list_uri(
                                    "https://programs.api.yle.fi/v3/schema/v3/packages/30-5629"
                                    "/latest?episodes_as_series=true"
                                )
                            }
                        }
                    ],
                }
            ]
        }
    }
    rows = parse_package_rows(view)
    assert len(rows) == 1
    assert rows[0].title == "Suosituimmat"


def test_card_from_dict_series() -> None:
    """A series card becomes a podcast card with its image and duration."""
    data = _series_card()
    data["labels"].append(
        {"type": "duration", "formatted": "35 min", "raw": "PT2144S", "rawType": "duration"}
    )
    card = card_from_dict(data)
    assert card is not None
    assert card.card_type == "series"
    assert card.item_id == "1-61894955"
    assert card.series_id == "1-61894955"
    assert card.duration == 2144
    assert card.image_url is not None
    assert not is_video_card(card)


def test_card_from_dict_program_uses_series_link() -> None:
    """An episode card carries the series it belongs to."""
    data = {
        "title": "Millainen terapeutti tekoäly on?",
        "pointer": {"type": "program", "uri": "yleareena://items/1-78377708"},
        "labels": [
            {"type": "itemId", "raw": "1-78377708"},
            {"type": "seriesLink", "raw": "1-61894955"},
            {"type": "itemtype", "raw": "https://schema.org/RadioEpisode"},
        ],
    }
    card = card_from_dict(data)
    assert card is not None
    assert card.card_type == "program"
    assert card.item_id == "1-78377708"
    assert card.series_id == "1-61894955"


def test_card_from_dict_package() -> None:
    """A package card points at another browse folder."""
    data = {
        "title": "Alueet",
        "pointer": {"type": "package", "uri": "yleareena://packages/57-V9M83DyPE"},
        "labels": [{"type": "itemId", "raw": "57-V9M83DyPE"}],
    }
    card = card_from_dict(data)
    assert card is not None
    assert card.card_type == "package"
    assert card.item_id == "57-V9M83DyPE"


def test_video_cards_are_detected() -> None:
    """Video podcasts are recognised by item type, highlight or media type."""
    video = _series_card()
    video["labels"] = [
        {"type": "itemtype", "raw": "https://schema.org/TVSeries"},
        {"type": "itemId", "raw": "1-63361128"},
    ]
    card = card_from_dict(video)
    assert card is not None
    assert is_video_card(card)

    highlighted = _series_card()
    highlighted["labels"].append({"type": "highlight", "raw": "VIDEOPODCAST"})
    card = card_from_dict(highlighted)
    assert card is not None
    assert is_video_card(card)


def test_is_video_title() -> None:
    """The video category titles are recognised in both content languages."""
    assert is_video_title("Videopodcastit")
    assert is_video_title("Videopoddar")
    assert not is_video_title("Uutiset ja ajankohtaiset")


def test_parse_cards_and_count() -> None:
    """Cards are read from the response data list and the total count is exposed."""
    payload = {
        "meta": {"offset": 0, "limit": 30, "count": 81},
        "data": [_series_card(), {"pointer": {"type": "unknown"}}],
    }
    cards = parse_cards(payload)
    assert len(cards) == 1
    assert cards[0].title == "Uutispodcast"
    assert content_page_count(payload) == 81
    assert content_page_count({"data": []}) == 0
