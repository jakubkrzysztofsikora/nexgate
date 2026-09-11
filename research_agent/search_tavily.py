"""One bounded Tavily search path; search snippets never become evidence."""

import os

from .model_litellm import AdapterError, post_json
from .sources import SourceClient


class TavilySearch:
    name = 'tavily'

    def __init__(self, *, api_key=None, endpoint='https://api.tavily.com/search', client=None):
        self.api_key = api_key or os.environ.get('TAVILY_API_KEY', '')
        self.endpoint = endpoint
        self.client = client or SourceClient()

    async def search(self, queries, policy):
        if not self.api_key:
            raise AdapterError('search credentials missing')
        if not 1 <= len(queries) <= policy.max_queries_per_iteration:
            raise AdapterError('query budget exceeded')
        urls = []
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
            for row in results:
                url = row.get('url') if isinstance(row, dict) else None
                if not isinstance(url, str) or len(url) > 2048:
                    raise AdapterError('invalid search result URL')
                if url not in urls and len(urls) < policy.max_sources:
                    urls.append(url)
        return urls
