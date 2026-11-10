"""Yle Areena podcast provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemTranscriptCue,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    DEFAULT_AUDIOBOOK_PODCAST_GENRE,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    find_episode_stream_url,
    find_episode_transcripts,
    get_cached_podcast,
    get_episode_positions,
    get_episode_transcript,
    parse_podcast,
    parse_podcast_episode,
    refresh_cached_podcast,
)
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.yle_areena.constants import (
    API_BASE_URL,
    API_VERSION,
    BROWSE_MENU_CACHE_TTL,
    BROWSE_MENU_PATH,
    BROWSER_HEADERS,
    CATEGORY_PATH,
    CLIENT_NAME,
    CONF_LANGUAGE,
    CONTENT_PAGE_CACHE_TTL,
    CREDENTIALS_CACHE_TTL,
    DEFAULT_LANGUAGE,
    DOMAIN,
    FINLAND_COUNTRY_CODE,
    FINLAND_ONLY_REGIONS,
    GEO_LOCATION_CACHE_TTL,
    GEO_LOCATION_URL,
    LIBRARY_PATH,
    MAX_SEARCH_RESULTS,
    PACKAGE_VIEW_CACHE_TTL,
    PACKAGE_VIEW_PATH,
    PAGE_SIZE,
    PLAYER_LANGUAGES,
    PLAYER_PREVIEW_PARAMS,
    PLAYER_PREVIEW_URL,
    PODCAST_PAGE_URLS,
    PREVIEW_CACHE_TTL,
    SEARCH_CACHE_TTL,
    SEARCH_PATH,
    SERVICE_RADIO,
    SUPPORTED_LANGUAGES,
)
from music_assistant.providers.yle_areena.helpers import (
    YleCard,
    YleCredentials,
    YlePreview,
    YleRow,
    content_page_count,
    is_program_id,
    is_series_id,
    is_video_card,
    is_video_title,
    parse_cards,
    parse_category_controls,
    parse_country_code,
    parse_credentials,
    parse_episode_without_enclosure,
    parse_package_rows,
    parse_preview,
    podcast_page_url,
    series_feed_url,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS,
}

# Kept as tuples so the source also parses on Python versions before PEP 758.
_NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError)
_RETRY_AFTER_PARSE_ERRORS = (TypeError, ValueError)

CONF_ENTRY_LIBRARY_SYNC_PODCASTS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_PODCASTS.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YleAreenaProvider(mass, manifest, config, SUPPORTED_FEATURES)


class YleAreenaProvider(MusicProvider):
    """Provider for the Yle Areena podcast catalog."""

    throttler: ThrottlerManager
    language: str
    _credentials: YleCredentials | None
    _credentials_fetched_at: float
    _credentials_lock: asyncio.Lock

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream streams."""
        return None

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as the catalog is not the same as the library contents."""
        return True

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (
            CONF_ENTRY_LIBRARY_SYNC_PODCASTS_HIDDEN,
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_LANGUAGE,
                type=ConfigEntryType.STRING,
                required=True,
                options=[
                    ConfigValueOption("fi"),
                    ConfigValueOption("sv"),
                ],
                default_value=DEFAULT_LANGUAGE,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        language = str(self.config.get_value(CONF_LANGUAGE) or DEFAULT_LANGUAGE)
        self.language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
        self.throttler = ThrottlerManager(rate_limit=20, period=60)
        self._credentials = None
        self._credentials_fetched_at = 0.0
        self._credentials_lock = asyncio.Lock()

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        if MediaType.PODCAST not in media_types:
            return result
        limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        payload = await self._search_podcasts(search_query, limit, self.language)
        result.podcasts = [
            podcast
            for card in parse_cards(payload)
            if (podcast := self._podcast_from_card(card)) is not None
        ]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, e.g. ``yle_areena://category/57-...``.
        """
        if not path.startswith(f"{DOMAIN}://"):
            msg = f"Invalid path for {DOMAIN} provider: {path}"
            raise KeyError(msg)
        parts = [part for part in path.split("://", 1)[1].split("/") if part]
        if not parts:
            return await self._browse_root()
        if parts[0] == LIBRARY_PATH and len(parts) == 1:
            return await self._browse_library()
        if parts[0] == CATEGORY_PATH:
            if len(parts) == 2:
                return await self._browse_category(parts[1])
            if len(parts) in (4, 5) and parts[2] == "row":
                row_index = self._parse_path_int(parts[3])
                offset = self._parse_path_int(parts[4]) if len(parts) == 5 else 0
                return await self._browse_row(parts[1], row_index, offset)
        msg = f"Invalid path for {DOMAIN} provider: {path}"
        raise KeyError(msg)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """
        Get library podcasts.

        The provider has no library of its own: it syncs the podcasts the user added
        to the Music Assistant library via search or browse.
        """
        async for podcast in self.mass.music.podcasts.iter_library_items_by_prov_id(
            self.instance_id
        ):
            mapping = self._mapping_for_podcast(podcast)
            if mapping is None:
                continue
            series_id = mapping.item_id
            if not is_series_id(series_id):
                self.logger.warning(
                    "Podcast %s has an invalid Yle Areena id: %s", podcast.name, series_id
                )
                continue
            try:
                parsed_feed = await refresh_cached_podcast(
                    mass=self.mass,
                    provider_instance_id=self.instance_id,
                    feed_url=series_feed_url(series_id, self.language),
                )
            except MediaNotFoundError:
                # Do not drop the podcast from the library just because Yle is unavailable.
                self.logger.warning("Was unable to sync podcast %s (%s)", podcast.name, series_id)
                podcast.item_id = series_id
                podcast.provider_mappings = {mapping}
                yield podcast
                continue
            yield parse_podcast(
                feed_url=series_feed_url(series_id, self.language),
                parsed_feed=parsed_feed,
                instance_id=self.instance_id,
                domain=self.domain,
                mass_item_id=series_id,
            )

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        parsed_feed = await self._cache_get_podcast(prov_podcast_id)
        return parse_podcast(
            feed_url=series_feed_url(prov_podcast_id, self.language),
            parsed_feed=parsed_feed,
            instance_id=self.instance_id,
            domain=self.domain,
            mass_item_id=prov_podcast_id,
        )

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        parsed_feed = await self._cache_get_podcast(prov_podcast_id)
        podcast_cover = parsed_feed.get("cover_url")
        episodes = parsed_feed.get("episodes", [])
        positions = get_episode_positions(episodes)
        for position, episode in zip(positions, episodes, strict=True):
            if mass_episode := self._parse_episode(
                episode=episode,
                prov_podcast_id=prov_podcast_id,
                position=position,
                podcast_cover=podcast_cover,
                podcast_name=parsed_feed.get("title"),
            ):
                yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        series_id, guid_or_stream_url = self._split_episode_id(prov_episode_id)
        parsed_feed = await self._cache_get_podcast(series_id)
        podcast_cover = parsed_feed.get("cover_url")
        episodes = parsed_feed.get("episodes", [])
        positions = get_episode_positions(episodes)
        for position, episode in zip(positions, episodes, strict=True):
            mass_episode = self._parse_episode(
                episode=episode,
                prov_podcast_id=series_id,
                position=position,
                podcast_cover=podcast_cover,
                podcast_name=parsed_feed.get("title"),
            )
            if mass_episode is None:
                continue
            _, episode_guid = mass_episode.item_id.split(" ", 1)
            if episode_guid == guid_or_stream_url:
                await enrich_episode_chapters(
                    session=self.mass.http_session,
                    chapters_json_url=episode.get("chapters_json_url"),
                    mass_episode=mass_episode,
                )
                return mass_episode
        msg = f"Episode not found: {prov_episode_id}"
        raise MediaNotFoundError(msg)

    async def get_podcast_episode_transcript(
        self, prov_episode_id: str
    ) -> tuple[str | None, list[MediaItemTranscriptCue] | None]:
        """Get the transcript for a podcast episode."""
        series_id, guid_or_stream_url = self._split_episode_id(prov_episode_id)
        parsed_feed = await self._cache_get_podcast(series_id)
        return await get_episode_transcript(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            transcripts=find_episode_transcripts(
                parsed_feed=parsed_feed, guid_or_stream_url=guid_or_stream_url
            ),
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a podcast episode."""
        series_id, guid_or_stream_url = self._split_episode_id(item_id)
        parsed_feed = await self._cache_get_podcast(series_id)
        stream_url = find_episode_stream_url(
            parsed_feed=parsed_feed, guid_or_stream_url=guid_or_stream_url
        )
        if stream_url is None:
            # the feed carries no enclosure for this episode, so the stream is
            # resolved from the Areena player instead
            stream_url = await self._resolve_stream_url(guid_or_stream_url)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(stream_url),
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    async def _browse_root(self) -> list[BrowseFolder]:
        """Return the root browse listing with the library and podcast categories."""
        folders = [
            BrowseFolder(
                item_id=LIBRARY_PATH,
                provider=self.instance_id,
                path=f"{DOMAIN}://{LIBRARY_PATH}",
                name="",
                translation_key="library",
            )
        ]
        menu = await self._get_browse_menu_cached(self.language)
        for category in parse_category_controls(menu):
            folders.append(
                BrowseFolder(
                    item_id=category.package_id,
                    provider=self.instance_id,
                    path=f"{DOMAIN}://{CATEGORY_PATH}/{category.package_id}",
                    name=category.title,
                    image=(
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=category.image_url,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                        if category.image_url
                        else None
                    ),
                )
            )
        return folders

    async def _browse_library(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Return the podcasts the user added to the Music Assistant library."""
        if podcasts := await self.mass.music.podcasts.library_items(
            provider=self.instance_id,
            summary=False,
        ):
            return podcasts
        return [podcast async for podcast in self.get_library_podcasts()]

    async def _browse_category(
        self, package_id: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Return the content rows of a podcast category."""
        rows = await self._get_category_rows(package_id)
        if not rows:
            return []
        if len(rows) == 1:
            return await self._browse_row(package_id, rows[0].index, 0)
        return [
            BrowseFolder(
                item_id=f"{package_id}:{row.index}",
                provider=self.instance_id,
                path=f"{DOMAIN}://{CATEGORY_PATH}/{package_id}/row/{row.index}",
                name=row.title,
            )
            for row in rows
        ]

    async def _browse_row(
        self, package_id: str, row_index: int, offset: int
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Return one page of a category content row."""
        rows = await self._get_category_rows(package_id)
        row = next((row for row in rows if row.index == row_index), None)
        if row is None:
            msg = f"Unknown row {row_index} for package {package_id}"
            raise KeyError(msg)
        payload = await self._get_content_page(row.source_uri, offset)
        cards = parse_cards(payload)
        items: list[MediaItemType | BrowseFolder] = []
        for card in cards:
            if item := self._browse_item_from_card(card):
                items.append(item)
        next_offset = offset + len(cards)
        if cards and next_offset < content_page_count(payload):
            items.append(
                BrowseFolder(
                    item_id=f"{package_id}:{row_index}:{next_offset}",
                    provider=self.instance_id,
                    path=(f"{DOMAIN}://{CATEGORY_PATH}/{package_id}/row/{row_index}/{next_offset}"),
                    name="",
                    translation_key="load_more",
                )
            )
        return items

    @use_cache(BROWSE_MENU_CACHE_TTL)
    async def _get_browse_menu_cached(self, language: str) -> dict[str, Any]:
        """Return the cached Areena podcast browse menu."""
        return await self._api_get(
            f"{API_BASE_URL}{BROWSE_MENU_PATH}",
            {"language": language},
        )

    async def _get_category_rows(self, package_id: str) -> list[YleRow]:
        """Return the usable content rows of a category package."""
        view = await self._get_package_view(package_id, self.language)
        rows = parse_package_rows(view)
        data = view.get("data")
        package_title = data.get("title") if isinstance(data, dict) else None
        fallback_title = package_title if isinstance(package_title, str) else ""
        for row in rows:
            if not row.title:
                row.title = fallback_title
        return rows

    @use_cache(PACKAGE_VIEW_CACHE_TTL)
    async def _get_package_view(self, package_id: str, language: str) -> dict[str, Any]:
        """Return the cached package view of a category."""
        return await self._api_get(
            f"{API_BASE_URL}{PACKAGE_VIEW_PATH.format(package_id=package_id)}",
            {
                "language": language,
                "v": API_VERSION,
                "client": CLIENT_NAME,
            },
        )

    @use_cache(CONTENT_PAGE_CACHE_TTL)
    async def _get_content_page(self, source_uri: str, offset: int) -> dict[str, Any]:
        """Return one page of a tokenized content list."""
        separator = "&" if "?" in source_uri else "?"
        query = urlencode({"offset": offset, "limit": PAGE_SIZE})
        return await self._api_get(f"{source_uri}{separator}{query}")

    @use_cache(SEARCH_CACHE_TTL)
    async def _search_podcasts(
        self, search_query: str, limit: int, language: str
    ) -> dict[str, Any]:
        """Search the Areena catalog for podcast series."""
        return await self._api_get(
            f"{API_BASE_URL}{SEARCH_PATH}",
            {
                "language": language,
                "client": CLIENT_NAME,
                "v": API_VERSION,
                "episodes": "false",
                "packages": "false",
                "query": search_query,
                "service": SERVICE_RADIO,
                "limit": limit,
                "offset": 0,
            },
        )

    def _browse_item_from_card(self, card: YleCard) -> MediaItemType | BrowseFolder | None:
        """Turn a browse card into a podcast or a nested category folder."""
        if card.card_type == "series":
            return self._podcast_from_card(card)
        if card.card_type == "package" and not is_video_title(card.title):
            return BrowseFolder(
                item_id=card.item_id,
                provider=self.instance_id,
                path=f"{DOMAIN}://{CATEGORY_PATH}/{card.item_id}",
                name=card.title or card.item_id,
                image=(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=card.image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                    if card.image_url
                    else None
                ),
            )
        return None

    def _podcast_from_card(self, card: YleCard) -> Podcast | None:
        """Turn a series card into a Podcast."""
        if card.card_type != "series" or is_video_card(card):
            return None
        item_id = card.item_id
        if not is_series_id(item_id):
            return None
        podcast = Podcast(
            item_id=item_id,
            provider=self.instance_id,
            name=card.title or item_id,
            uri=podcast_page_url(item_id),
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        podcast.metadata.description = card.description
        podcast.metadata.genres = {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
        podcast.metadata.links = {
            MediaItemLink(type=LinkType.WEBSITE, url=podcast_page_url(item_id))
        }
        if card.image_url:
            podcast.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=card.image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return podcast

    def _mapping_for_podcast(self, podcast: Podcast) -> ProviderMapping | None:
        """Return this provider's mapping on a library podcast."""
        return next(
            (
                mapping
                for mapping in podcast.provider_mappings
                if mapping.provider_instance == self.instance_id
            ),
            None,
        )

    async def _cache_get_podcast(self, series_id: str) -> dict[str, Any]:
        """Return the cached parsed feed of a podcast."""
        if not is_series_id(series_id):
            msg = f"Invalid Yle Areena series id: {series_id}"
            raise MediaNotFoundError(msg)
        return await get_cached_podcast(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            feed_url=series_feed_url(series_id, self.language),
        )

    def _split_episode_id(self, prov_episode_id: str) -> tuple[str, str]:
        """Split an episode item id into its series id and episode guid."""
        series_id, separator, guid_or_stream_url = prov_episode_id.partition(" ")
        if not separator or not guid_or_stream_url or not is_series_id(series_id):
            msg = f"Invalid Yle Areena episode id: {prov_episode_id}"
            raise MediaNotFoundError(msg)
        return series_id, guid_or_stream_url

    def _parse_episode(
        self,
        *,
        episode: dict[str, Any],
        prov_podcast_id: str,
        position: int,
        podcast_cover: str | None,
        podcast_name: str | None,
    ) -> PodcastEpisode | None:
        """Turn a feed episode into a podcast episode."""
        mass_episode = parse_podcast_episode(
            episode=episode,
            prov_podcast_id=prov_podcast_id,
            position=position,
            podcast_cover=podcast_cover,
            podcast_name=podcast_name,
            domain=self.domain,
            instance_id=self.instance_id,
        )
        if mass_episode is not None:
            return mass_episode
        # Yle publishes no enclosure for a part of the catalogue, e.g. for music
        # programmes. Those episodes are listed as well, their stream is resolved
        # from the Areena player when the playback starts.
        return parse_episode_without_enclosure(
            episode=episode,
            prov_podcast_id=prov_podcast_id,
            position=position,
            podcast_cover=podcast_cover,
            podcast_name=podcast_name,
            domain=self.domain,
            instance_id=self.instance_id,
        )

    async def _resolve_stream_url(self, program_id: str) -> str:
        """Resolve the stream url of an episode that the feed has no enclosure for."""
        if not is_program_id(program_id):
            msg = f"Invalid Yle Areena programme id: {program_id}"
            raise MediaNotFoundError(msg)
        preview = await self._get_preview(program_id)
        if preview is None:
            msg = f"No stream found for episode: {program_id}"
            raise MediaNotFoundError(msg)
        if preview.is_content_protected:
            # there is no licence path for such a programme, so it is skipped
            msg = f"Yle Areena episode {program_id} is content protected"
            raise MediaNotFoundError(msg)
        await self._ensure_region_allowed(preview.region)
        return preview.stream_url

    @use_cache(PREVIEW_CACHE_TTL)
    async def _get_preview_payload(self, program_id: str, language: str) -> dict[str, Any]:
        """Return the cached raw player preview response of an episode."""
        params = dict(PLAYER_PREVIEW_PARAMS)
        params["language"] = PLAYER_LANGUAGES.get(language, "fin")
        return await self._player_api_get(PLAYER_PREVIEW_URL.format(program_id=program_id), params)

    async def _get_preview(self, program_id: str) -> YlePreview | None:
        """Return the playback details of an episode."""
        payload = await self._get_preview_payload(program_id, self.language)
        return parse_preview(payload, self.language)

    async def _ensure_region_allowed(self, region: str | None) -> None:
        """Refuse to stream content that Yle limits to a region this server is not in."""
        if region not in FINLAND_ONLY_REGIONS:
            return
        country_code = await self._get_country_code()
        if country_code is None:
            # the location could not be determined, the stream itself is still
            # refused by Yle when it is not allowed from here
            self.logger.warning("Could not determine the country of this server")
            return
        if country_code != FINLAND_COUNTRY_CODE:
            msg = (
                "Yle Areena content is only available in Finland "
                f"(this server is in {country_code})"
            )
            raise ProviderUnavailableError(msg)

    @use_cache(GEO_LOCATION_CACHE_TTL)
    async def _get_country_code(self) -> str | None:
        """Return the country code Yle sees for this server."""
        payload = await self._api_get(GEO_LOCATION_URL)
        return parse_country_code(payload)

    @throttle_with_retries
    async def _player_api_get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Perform a throttled request against the Areena player API."""
        try:
            async with self.mass.http_session.get(
                url, params=params, headers=BROWSER_HEADERS
            ) as response:
                status = response.status
                if status == 404:
                    msg = f"Yle Areena item not found: {url}"
                    raise MediaNotFoundError(msg)
                if status == 429:
                    raise RateLimited(
                        "Yle Areena rate limit",
                        backoff_time=self._retry_after(response.headers.get("Retry-After")),
                    )
                if status >= 500:
                    msg = f"Yle Areena API error {status}"
                    raise ResourceTemporarilyUnavailable(msg)
                if status != 200:
                    msg = f"Yle Areena API error {status}"
                    raise ProviderUnavailableError(msg)
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    msg = "Invalid JSON from Yle Areena"
                    raise ResourceTemporarilyUnavailable(msg) from err
        except _NETWORK_ERRORS as err:
            msg = "Unable to reach Yle Areena"
            raise ResourceTemporarilyUnavailable(msg) from err
        if not isinstance(payload, dict):
            msg = "Unexpected response from Yle Areena"
            raise ResourceTemporarilyUnavailable(msg)
        return payload

    async def _get_credentials(self, force_refresh: bool = False) -> YleCredentials:
        """Return the frontend API credentials, refreshing them when stale."""
        now = time.monotonic()
        if (
            not force_refresh
            and self._credentials is not None
            and now - self._credentials_fetched_at < CREDENTIALS_CACHE_TTL
        ):
            return self._credentials
        async with self._credentials_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._credentials is not None
                and now - self._credentials_fetched_at < CREDENTIALS_CACHE_TTL
            ):
                return self._credentials
            credentials = await self._fetch_credentials()
            self._credentials = credentials
            self._credentials_fetched_at = now
            return credentials

    async def _fetch_credentials(self) -> YleCredentials:
        """Read the frontend API credentials from the podcast landing page."""
        url = PODCAST_PAGE_URLS[self.language]
        try:
            async with self.mass.http_session.get(url, headers=BROWSER_HEADERS) as response:
                if response.status != 200:
                    msg = f"Yle Areena returned HTTP {response.status} for {url}"
                    raise ProviderUnavailableError(msg)
                html = await response.text()
        except aiohttp.ClientError as err:
            msg = "Unable to reach Yle Areena"
            raise ProviderUnavailableError(msg) from err
        credentials = parse_credentials(html)
        if credentials is None:
            msg = "Could not read the Yle Areena API credentials"
            raise ProviderUnavailableError(msg)
        return credentials

    @throttle_with_retries
    async def _api_get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform a throttled Yle Areena API request."""
        credentials = await self._get_credentials()
        payload = await self._perform_request(url, params, credentials)
        if payload is None:
            credentials = await self._get_credentials(force_refresh=True)
            payload = await self._perform_request(url, params, credentials)
            if payload is None:
                msg = "Yle Areena refused the API request"
                raise ProviderUnavailableError(msg)
        return payload

    async def _perform_request(
        self,
        url: str,
        params: dict[str, Any] | None,
        credentials: YleCredentials,
    ) -> dict[str, Any] | None:
        """Perform one API request, returning None when the credentials were rejected."""
        request_params = dict(params or {})
        request_params["app_id"] = credentials.app_id
        request_params["app_key"] = credentials.app_key
        try:
            async with self.mass.http_session.get(
                url, params=request_params, headers=BROWSER_HEADERS
            ) as response:
                status = response.status
                if status == 403:
                    return None
                if status == 404:
                    msg = f"Yle Areena item not found: {url}"
                    raise MediaNotFoundError(msg)
                if status == 429:
                    raise RateLimited(
                        "Yle Areena rate limit",
                        backoff_time=self._retry_after(response.headers.get("Retry-After")),
                    )
                if status >= 500:
                    msg = f"Yle Areena API error {status}"
                    raise ResourceTemporarilyUnavailable(msg)
                if status != 200:
                    msg = f"Yle Areena API error {status}"
                    raise ProviderUnavailableError(msg)
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    msg = "Invalid JSON from Yle Areena"
                    raise ResourceTemporarilyUnavailable(msg) from err
        except _NETWORK_ERRORS as err:
            msg = "Unable to reach Yle Areena"
            raise ResourceTemporarilyUnavailable(msg) from err
        if not isinstance(payload, dict):
            msg = "Unexpected response from Yle Areena"
            raise ResourceTemporarilyUnavailable(msg)
        return payload

    @staticmethod
    def _retry_after(value: str | None) -> int:
        """Return the number of seconds a Retry-After header asks to wait."""
        if not value:
            return 60
        try:
            return max(1, int(value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
        except _RETRY_AFTER_PARSE_ERRORS:
            return 60
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(1, int((when - utc()).total_seconds()))

    @staticmethod
    def _parse_path_int(value: str) -> int:
        """Parse a browse path segment as a non-negative integer."""
        try:
            parsed = int(value)
        except ValueError:
            msg = f"Invalid browse path segment: {value}"
            raise KeyError(msg) from None
        if parsed < 0:
            msg = f"Invalid browse path segment: {value}"
            raise KeyError(msg)
        return parsed
