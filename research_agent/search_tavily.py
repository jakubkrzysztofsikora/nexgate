"""One bounded Tavily search path; search snippets never become evidence."""

import os
from collections import deque

from .model_litellm import AdapterError, post_json
from .sources import SourceClient


class TavilySearch:
    name = 'tavily'

    def __init__(self, *, api_key=None, endpoint='https://api.tavily.com/search', client=None):
        self.api_key = api_key or os.environ.get('TAVILY_API_KEY', '')
        self.endpoint = endpoint
        self.client = client or SourceClient()

    async def search(self, queries, policy, *, remaining_sources=None, excluded_urls=()):
        if not self.api_key:
            raise AdapterError('search credentials missing')
        if not 1 <= len(queries) <= policy.max_queries_per_iteration:
            raise AdapterError('query budget exceeded')
        capacity = policy.max_sources if remaining_sources is None else remaining_sources
        if type(capacity) is not int or not 0 <= capacity <= policy.max_sources:
            raise AdapterError('invalid remaining source capacity')
        categories = []
        seen = set(excluded_urls)
        for query in queries:
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                raise AdapterError('invalid search query')
            response = await post_json(self.endpoint, {
                'query': query, 'search_depth': 'basic', 'max_results': min(policy.max_sources, 20),
                'include_answer': False, 'include_raw_content': False,
            }, self.api_key, policy)
            results = response.get('results')
            if not isinstance(results, list) or len(results) > min(policy.max_sources, 20):
                raise AdapterError('invalid search result count')
            category = deque()
            for row in results:
                url = row.get('url') if isinstance(row, dict) else None
                if not isinstance(url, str) or len(url) > 2048:
                    raise AdapterError('invalid search result URL')
                if url not in seen:
                    category.append(url)
            categories.append(category)
        urls = []
        # One new URL per category per round; seeds and prior captures use no slots.
        while len(urls) < capacity and any(categories):
            for category in categories:
                while category and category[0] in seen:
                    category.popleft()
                if category and len(urls) < capacity:
                    url = category.popleft()
                    seen.add(url)
                    urls.append(url)
        return urls
