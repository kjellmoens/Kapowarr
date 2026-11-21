# -*- coding: utf-8 -*-

"""
Newznab indexer client. Searches a Newznab compatible API (a usenet indexer
like NZBGeek or NZBFinder, or an indexer proxied by Prowlarr/NZBHydra2) for
NZB files.
"""

from asyncio import run
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Tuple, Union
from xml.etree.ElementTree import Element, ParseError, fromstring

from aiohttp import ClientError

from backend.base.custom_exceptions import ClientNotWorking, CredentialInvalid
from backend.base.definitions import (BrokenClientReason, DownloadType,
                                      IndexerClientField as ICF, QueryResult,
                                      SearchQuery, SearchResultData)
from backend.base.file_extraction import extract_filename_data
from backend.base.helpers import AsyncSession
from backend.base.logging import LOGGER
from backend.implementations.indexer_client_manager import (BaseIndexerClient,
                                                            IndexerClients)

DEFAULT_CATEGORIES = "7030"
"Newznab category Books > Comics"

PAGE_SIZE = 100

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"

NEWZNAB_AUTH_ERRORS = ("100", "101", "102")
"""
Newznab error codes for incorrect user credentials, account suspended and
insufficient privileges
"""


def api_url(url: str) -> str:
    """The URL of the API endpoint of a Newznab indexer.

    Args:
        url (str): The URL of the indexer, with or without '/api'.

    Returns:
        str: The URL of the API endpoint.
    """
    url = url.rstrip('/')
    if url.endswith('/api'):
        return url
    return url + '/api'


def _newznab_error(root: Element) -> Union[Tuple[str, str], None]:
    """Get the Newznab error in a response, if it is one.

    Args:
        root (Element): The root of the XML response.

    Returns:
        Union[Tuple[str, str], None]: The error code and description, or
            `None` if the response isn't an error.
    """
    if root.tag != 'error':
        return None
    return (root.get('code') or '', root.get('description') or '')


def _parse_items(
    root: Element
) -> Tuple[List[Dict[str, Any]], Union[int, None], Union[int, None]]:
    """Get the releases out of a Newznab search response.

    Args:
        root (Element): The root of the XML (RSS) response.

    Returns:
        Tuple[List[Dict[str, Any]], Union[int, None], Union[int, None]]:
            The releases (title, link, size, pub_date), the offset and the
            total number of results, if the indexer supplied them.
    """
    channel = root.find('channel')
    if channel is None:
        return [], None, None

    offset, total = None, None
    response = channel.find(f'{NEWZNAB_NS}response')
    if response is not None:
        try:
            offset = int(response.get('offset') or 0)
            total = int(response.get('total') or 0)
        except ValueError:
            pass

    items: List[Dict[str, Any]] = []
    for item in channel.findall('item'):
        title = (item.findtext('title') or '').strip()

        link = (item.findtext('link') or '').strip()
        enclosure = item.find('enclosure')
        if enclosure is not None and enclosure.get('url'):
            link = enclosure.get('url') or link

        if not title or not link:
            continue

        size = -1
        attrs = {
            attr.get('name'): attr.get('value')
            for attr in item.findall(f'{NEWZNAB_NS}attr')
        }
        for candidate in (
            attrs.get('size'),
            item.findtext('size'),
            enclosure.get('length') if enclosure is not None else None
        ):
            try:
                if candidate and int(candidate) > 0:
                    size = int(candidate)
                    break
            except ValueError:
                continue

        pub_date = None
        raw_date = item.findtext('pubDate')
        if raw_date:
            try:
                pub_date = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                pub_date = None

        items.append({
            'title': title,
            'link': link,
            'size': size,
            'pub_date': pub_date
        })

    return items, offset, total


@IndexerClients.register_client(
    DownloadType.USENET, 'Newznab',
    (ICF.TITLE, ICF.ENABLED, ICF.URL, ICF.API_KEY, ICF.CATEGORIES),
    allow_multiple_instances=True
)
class NewznabIndexer(BaseIndexerClient):
    def __init__(self, indexer_id: int) -> None:
        super().__init__(indexer_id)
        self.session: Union[AsyncSession, None] = None
        return

    @property
    def _category_param(self) -> str:
        return (self._categories or DEFAULT_CATEGORIES).replace(' ', '')

    def _to_search_result(self, item: Dict[str, Any]) -> SearchResultData:
        return {
            **extract_filename_data(
                item['title'],
                assume_volume_number=False,
                fix_year=True
            ),
            "link": item['link'],
            "display_title": item['title'],
            "size": item['size'],
            "indexer_id": self._id,
            "indexer_title": self._title
        }

    async def _request(self, params: Dict[str, Any]) -> Union[Element, None]:
        if not self.session:
            self.session = AsyncSession()

        text = await self.session.get_text(
            api_url(self._url),
            params={
                **params,
                'apikey': self._api_key or '',
                'cat': self._category_param
            },
            quiet_fail=True
        )
        if not text:
            return None

        try:
            root = fromstring(text)
        except ParseError:
            LOGGER.warning(
                f"Newznab indexer {self._title} returned an invalid response"
            )
            return None

        error = _newznab_error(root)
        if error:
            LOGGER.warning(
                f"Newznab indexer {self._title} returned error {error[0]}: "
                f"{error[1]}"
            )
            return None

        return root

    async def search(self, query: SearchQuery) -> QueryResult:
        offset = (query['page'] - 1) * PAGE_SIZE
        root = await self._request({
            't': 'search',
            'q': query['query'],
            'offset': offset,
            'limit': PAGE_SIZE,
            'extended': 1
        })
        if root is None:
            return QueryResult([], next_page_available=False)

        items, response_offset, total = _parse_items(root)
        if total is not None:
            next_page_available = (
                (response_offset or offset) + len(items) < total
            )
        else:
            next_page_available = len(items) >= PAGE_SIZE

        return QueryResult(
            [self._to_search_result(item) for item in items],
            next_page_available=next_page_available
        )

    async def discover(self, last_check: datetime) -> List[SearchResultData]:
        root = await self._request({
            't': 'search',
            'limit': PAGE_SIZE,
            'extended': 1
        })
        if root is None:
            return []

        if last_check.tzinfo is None:
            last_check = last_check.replace(tzinfo=timezone.utc)

        items, _, _ = _parse_items(root)
        return [
            self._to_search_result(item)
            for item in items
            if item['pub_date'] is None or item['pub_date'] > last_check
        ]

    async def shutdown(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None
        return

    @classmethod
    async def __test(
        cls,
        url: str,
        api_key: Union[str, None],
        categories: Union[str, None]
    ) -> None:
        async with AsyncSession() as session:
            try:
                # Searching instead of asking for the capabilities, because
                # some indexers allow `t=caps` without (valid) API key.
                async with session.get(
                    api_url(url),
                    params={
                        't': 'search',
                        'q': 'test',
                        'limit': 1,
                        'apikey': api_key or '',
                        'cat': (categories or DEFAULT_CATEGORIES)
                            .replace(' ', '')
                    }
                ) as response:
                    status = response.status
                    text = await response.text()
            except ClientError:
                raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        if status in (401, 403):
            # E.g. Prowlarr answers a wrong API key with an empty 401
            raise CredentialInvalid

        try:
            root = fromstring(text)
        except ParseError:
            raise ClientNotWorking(BrokenClientReason.NOT_CLIENT_INSTANCE)

        error = _newznab_error(root)
        if error:
            if error[0] in NEWZNAB_AUTH_ERRORS:
                raise CredentialInvalid
            LOGGER.error(
                f"Newznab indexer returned error {error[0]}: {error[1]}"
            )
            raise ClientNotWorking(
                BrokenClientReason.FAILED_PROCESSING_RESPONSE
            )

        if root.tag != 'rss':
            raise ClientNotWorking(BrokenClientReason.NOT_CLIENT_INSTANCE)
        return

    @classmethod
    def test(cls, url: str, **extra_fields: Any) -> None:
        run(cls.__test(
            url,
            extra_fields.get(ICF.API_KEY.value),
            extra_fields.get(ICF.CATEGORIES.value)
        ))
        return
