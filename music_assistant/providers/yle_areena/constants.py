"""Constants for the Yle Areena provider."""

from __future__ import annotations

from typing import Final

DOMAIN: Final[str] = "yle_areena"

CONF_LANGUAGE: Final[str] = "language"
DEFAULT_LANGUAGE: Final[str] = "fi"
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("fi", "sv")

# The podcast landing pages embed the frontend API credentials in window.envVariables.
PODCAST_PAGE_URLS: Final[dict[str, str]] = {
    "fi": "https://areena.yle.fi/podcastit",
    "sv": "https://areena.yle.fi/sv/poddar",
}

API_BASE_URL: Final[str] = "https://areena.api.yle.fi"
BROWSE_MENU_PATH: Final[str] = "/v2/web/browseMenuContent"
PACKAGE_VIEW_PATH: Final[str] = "/v1/ui/views/packages/{package_id}.json"
SEARCH_PATH: Final[str] = "/v1/ui/search"
CLIENT_NAME: Final[str] = "yle-areena-web"
API_VERSION: Final[str] = "10"
SERVICE_RADIO: Final[str] = "radio"

FEED_URL_TEMPLATE: Final[str] = "https://feeds.yle.fi/areena/v1/series/{series_id}.rss"
IMAGE_URL_TEMPLATE: Final[str] = "https://img.img-cdn.yle.fi/crop_fill,ar_1:1,w_600/{image_id}"
PODCAST_PAGE_URL_TEMPLATE: Final[str] = "https://areena.yle.fi/{series_id}"

BROWSER_HEADERS: Final[dict[str, str]] = {"User-Agent": "Mozilla/5.0"}

CREDENTIALS_CACHE_TTL: Final[int] = 24 * 3600
BROWSE_MENU_CACHE_TTL: Final[int] = 6 * 3600
PACKAGE_VIEW_CACHE_TTL: Final[int] = 600
CONTENT_PAGE_CACHE_TTL: Final[int] = 600
SEARCH_CACHE_TTL: Final[int] = 24 * 3600

PAGE_SIZE: Final[int] = 30
MAX_SEARCH_RESULTS: Final[int] = 50

LIBRARY_PATH: Final[str] = "library"
CATEGORY_PATH: Final[str] = "category"

VIDEO_ITEM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "https://schema.org/TVSeries",
        "https://schema.org/TVEpisode",
    }
)
VIDEO_TITLE_MARKERS: Final[tuple[str, ...]] = ("videopodcast", "videopodd")
