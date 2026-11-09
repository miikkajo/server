"""Tests for the Yle Areena provider browse, search and playback logic."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Podcast, ProviderMapping

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers import yle_areena as yle_areena_module
from music_assistant.providers.yle_areena import SUPPORTED_FEATURES, YleAreenaProvider
from music_assistant.providers.yle_areena.constants import CONF_LANGUAGE, DOMAIN
from music_assistant.providers.yle_areena.helpers import YleRow

if TYPE_CHECKING:
    from music_assistant_models.media_items import BrowseFolder


@pytest.fixture
def provider() -> YleAreenaProvider:
    """Create a real provider instance with its network and cache dependencies mocked."""
    mass = Mock()
    mass.cache = AsyncMock()
    manifest = Mock()
    manifest.domain = DOMAIN
    config = Mock()
    config.instance_id = DOMAIN
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_LANGUAGE: "fi",
    }.get(key, default)
    instance = YleAreenaProvider(mass, manifest, config, SUPPORTED_FEATURES)
    instance.language = "fi"
    instance.throttler = ThrottlerManager(rate_limit=20, period=60)
    instance._credentials = None
    instance._credentials_fetched_at = 0.0
    instance._credentials_lock = asyncio.Lock()
    return instance


def _series_card(item_id: str, title: str) -> dict[str, Any]:
    """Build a series card as the browse and search endpoints return one."""
    return {
        "title": title,
        "description": "",
        "pointer": {"type": "series", "uri": f"yleareena://items/{item_id}"},
        "labels": [{"type": "itemId", "raw": item_id}],
    }


def _program_card(item_id: str) -> dict[str, Any]:
    """Build an episode card that a podcast provider cannot represent."""
    return {
        "title": "Episode",
        "pointer": {"type": "program", "uri": f"yleareena://items/{item_id}"},
        "labels": [{"type": "itemId", "raw": item_id}],
    }


def _menu() -> dict[str, Any]:
    """Build a browse menu with one usable podcast category."""
    return {
        "data": {
            "podcastBlocks": [
                {
                    "type": "categoryImageLinks",
                    "controls": [
                        {
                            "type": "navigator",
                            "title": "Lapset",
                            "destination": {"uri": "yleareena://packages/57-DDRw1lBwg"},
                        }
                    ],
                }
            ]
        }
    }


async def test_browse_root_lists_library_and_categories(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root lists the library entry first, followed by the podcast categories."""
    monkeypatch.setattr(provider, "_get_browse_menu_cached", AsyncMock(return_value=_menu()))
    folders = cast("list[BrowseFolder]", await provider.browse(f"{DOMAIN}://"))
    assert [folder.path for folder in folders] == [
        f"{DOMAIN}://library",
        f"{DOMAIN}://category/57-DDRw1lBwg",
    ]
    assert folders[0].translation_key == "library"
    assert folders[1].name == "Lapset"


async def test_browse_category_lists_rows(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A category with multiple rows exposes each row as a folder."""
    rows = [
        YleRow(index=0, title="Suosittelemme", source_uri="uri-0", source="source-0"),
        YleRow(index=1, title="Uusimmat", source_uri="uri-1", source="source-1"),
    ]
    monkeypatch.setattr(provider, "_get_category_rows", AsyncMock(return_value=rows))
    folders = cast("list[BrowseFolder]", await provider.browse(f"{DOMAIN}://category/57-DDRw1lBwg"))
    assert [folder.path for folder in folders] == [
        f"{DOMAIN}://category/57-DDRw1lBwg/row/0",
        f"{DOMAIN}://category/57-DDRw1lBwg/row/1",
    ]
    assert [folder.name for folder in folders] == ["Suosittelemme", "Uusimmat"]


async def test_browse_category_with_single_row_returns_items(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A category with a single row is skipped as an intermediate step."""
    rows = [YleRow(index=0, title="Kaikki", source_uri="uri-0", source="source-0")]
    payload = {
        "meta": {"count": 2},
        "data": [_series_card("1-61894955", "Uutispodcast"), _program_card("1-78588425")],
    }
    monkeypatch.setattr(provider, "_get_category_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(provider, "_get_content_page", AsyncMock(return_value=payload))
    items = cast("list[Podcast]", await provider.browse(f"{DOMAIN}://category/57-DDRw1lBwg"))
    assert [item.item_id for item in items] == ["1-61894955"]


async def test_browse_row_appends_load_more(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row with more items than one page ends in a translated load-more folder."""
    rows = [YleRow(index=0, title="Kaikki", source_uri="uri-0", source="source-0")]
    payload = {
        "meta": {"count": 40},
        "data": [_series_card("1-61894955", "Uutispodcast")],
    }
    monkeypatch.setattr(provider, "_get_category_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(provider, "_get_content_page", AsyncMock(return_value=payload))
    items = cast(
        "list[BrowseFolder]", await provider.browse(f"{DOMAIN}://category/57-DDRw1lBwg/row/0")
    )
    assert items[-1].translation_key == "load_more"
    assert items[-1].path == f"{DOMAIN}://category/57-DDRw1lBwg/row/0/1"


@pytest.mark.parametrize(
    "path",
    [
        f"{DOMAIN}://category",
        f"{DOMAIN}://category/57-DDRw1lBwg/row",
        f"{DOMAIN}://category/57-DDRw1lBwg/row/not-a-number",
        f"{DOMAIN}://category/57-DDRw1lBwg/row/0/1/extra",
        f"{DOMAIN}://unknown",
    ],
)
async def test_browse_rejects_invalid_paths(provider: YleAreenaProvider, path: str) -> None:
    """Malformed browse paths are rejected instead of returning an empty listing."""
    with pytest.raises(KeyError):
        await provider.browse(path)


async def test_browse_rejects_foreign_domain(provider: YleAreenaProvider) -> None:
    """A path from another provider is not handled."""
    with pytest.raises(KeyError):
        await provider.browse("spotify://library")


async def test_browse_library_returns_synced_podcasts(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library folder lists the podcasts that were synced from the library."""
    synced = Podcast(
        item_id="1-61894955",
        provider=DOMAIN,
        name="Uutispodcast",
        provider_mappings={
            ProviderMapping(
                item_id="1-61894955",
                provider_domain=DOMAIN,
                provider_instance=DOMAIN,
            )
        },
    )
    monkeypatch.setattr(
        provider.mass.music.podcasts, "library_items", AsyncMock(return_value=[synced])
    )
    items = await provider.browse(f"{DOMAIN}://library")
    assert list(items) == [synced]


async def test_get_library_podcasts_keeps_unavailable_feed(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feed that Yle cannot serve right now is kept instead of dropped from the library."""
    podcast = Podcast(
        item_id="123",
        provider="library",
        name="Uutispodcast",
        provider_mappings={
            ProviderMapping(
                item_id="1-61894955",
                provider_domain=DOMAIN,
                provider_instance=DOMAIN,
            )
        },
    )

    async def _library_items(*_args: Any, **_kwargs: Any) -> Any:
        yield podcast

    monkeypatch.setattr(
        provider.mass.music.podcasts,
        "iter_library_items_by_prov_id",
        _library_items,
    )
    monkeypatch.setattr(
        yle_areena_module,
        "refresh_cached_podcast",
        AsyncMock(side_effect=MediaNotFoundError("feed unavailable")),
    )
    synced = [item async for item in provider.get_library_podcasts()]
    assert [item.item_id for item in synced] == ["1-61894955"]
    assert synced[0].provider_mappings


async def test_search_returns_series_podcasts(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search turns series cards into podcasts and drops episode cards."""
    payload = {
        "data": [
            _series_card("1-61894955", "Uutispodcast"),
            _program_card("1-78588425"),
        ]
    }
    monkeypatch.setattr(provider, "_search_podcasts", AsyncMock(return_value=payload))
    result = await provider.search("uutispodcast", [MediaType.PODCAST], 5)
    podcasts = cast("list[Podcast]", result.podcasts)
    assert [podcast.item_id for podcast in podcasts] == ["1-61894955"]
    assert podcasts[0].provider_mappings
    assert podcasts[0].uri == "https://areena.yle.fi/1-61894955"


async def test_search_skips_other_media_types(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Areena catalog only contributes podcasts to a search."""
    search = AsyncMock()
    monkeypatch.setattr(provider, "_search_podcasts", search)
    result = await provider.search("uutispodcast", [MediaType.TRACK], 5)
    assert not result.podcasts
    search.assert_not_awaited()


async def test_get_stream_details_returns_enclosure(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The episode stream is the enclosure url of the matching feed episode."""
    parsed_feed = {
        "episodes": [
            {
                "title": "Episode",
                "guid": "1-78588425",
                "enclosures": [
                    {"url": "https://example.com/episode.mp3", "mime_type": "audio/mpeg"}
                ],
            }
        ]
    }
    monkeypatch.setattr(provider, "_cache_get_podcast", AsyncMock(return_value=parsed_feed))
    details = await provider.get_stream_details("1-61894955 1-78588425", MediaType.PODCAST_EPISODE)
    assert details.path == "https://example.com/episode.mp3"
    assert details.media_type == MediaType.PODCAST_EPISODE


async def test_get_stream_details_unknown_episode(
    provider: YleAreenaProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An episode that is no longer in the feed is reported as missing."""
    monkeypatch.setattr(provider, "_cache_get_podcast", AsyncMock(return_value={"episodes": []}))
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("1-61894955 1-78588425", MediaType.PODCAST_EPISODE)


@pytest.mark.parametrize(
    "episode_id",
    ["1-78588425", "1-61894955", "1-61894955 ", "not-a-series 1-78588425"],
)
async def test_invalid_episode_ids_are_rejected(
    provider: YleAreenaProvider, episode_id: str
) -> None:
    """Episode ids must carry both a valid series id and an episode guid."""
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode(episode_id)


async def test_config_entries_expose_language_choice(provider: YleAreenaProvider) -> None:
    """The language entry offers exactly the supported content languages."""
    entries = {entry.key: entry for entry in await provider.get_config_entries()}
    language = entries[CONF_LANGUAGE]
    assert language.default_value == "fi"
    assert [option.value for option in language.options] == ["fi", "sv"]
