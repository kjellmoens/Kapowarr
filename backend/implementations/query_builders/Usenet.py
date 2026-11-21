# -*- coding: utf-8 -*-

"""
Query builder for Newznab (usenet) indexers. Newznab searches match on words,
so the queries use plain words: no '#' or parentheses. Release names usually
pad issue numbers ("Series 039 (2020)"), so issue searches try both the padded
and the plain number.
"""

from typing import Tuple

from backend.base.definitions import (DownloadType, QueryBuilder,
                                      QueryKeys, SearchAction, SearchQuery,
                                      SpecialVersion)
from backend.implementations.query_builder_manager import QueryBuilders

TPB_FORMATS = (
    "{title} Vol {volume_number} TPB",
    "{title} TPB",
    "{title} {year}",
    "{title}"
)

VAI_FORMATS = (
    "{title} {year}",
    "{title}"
)

VOLUME_FORMATS = (
    "{title} {year}",
    "{title} Vol {volume_number}",
    "{title}"
)

SPECIFIC_ISSUE_FORMATS = (
    "{title} {padded_issue_number}",
    "{title} {issue_number}",
)

GENERAL_ISSUE_FORMATS = SPECIFIC_ISSUE_FORMATS + ("{title}",)


def _padded(issue_number: str) -> str:
    "Pad a whole issue number to three digits, like release names do"
    if issue_number.isdigit():
        return issue_number.zfill(3)
    return issue_number


def _unique(formats: Tuple[str, ...], issue_number: str) -> Tuple[str, ...]:
    "Drop the padded variation when it equals the plain one (e.g. issue 100)"
    if _padded(issue_number) == issue_number:
        return tuple(
            f for f in formats
            if "{padded_issue_number}" not in f
        )
    return formats


@QueryBuilders.register_builder(DownloadType.USENET)
class UsenetQueryBuilder(QueryBuilder):
    def next_query(
        self,
        search_action: SearchAction,
        query_keys: QueryKeys
    ) -> SearchQuery:
        super()._update_state(search_action)

        if query_keys.special_version == SpecialVersion.TPB:
            queries = TPB_FORMATS
        elif query_keys.special_version == SpecialVersion.VOLUME_AS_ISSUE:
            queries = VAI_FORMATS
        elif query_keys.issue_number is None:
            queries = VOLUME_FORMATS
        elif self.originally_volume_search:
            queries = _unique(SPECIFIC_ISSUE_FORMATS, query_keys.issue_number)
        else:
            queries = _unique(GENERAL_ISSUE_FORMATS, query_keys.issue_number)

        query = queries[min(self.query_variation_index, len(queries) - 1)]
        if query_keys.year is None:
            query = query.replace('{year}', '').strip()

        issue_number = query_keys.issue_number or ''
        result = query.format(
            title=query_keys.titles[self.alias_index],
            year=query_keys.year,
            volume_number=query_keys.volume_number,
            issue_number=issue_number,
            padded_issue_number=_padded(issue_number)
        )

        return {
            "query": " ".join(result.split()),
            "page": self.page,
            "total_available_variations": len(queries)
        }
