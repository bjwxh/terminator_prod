import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("NewsFetcher")
CHICAGO = ZoneInfo("America/Chicago")
BEIJING = ZoneInfo("Asia/Shanghai")

class NewsFetcher:
    def __init__(self, poll_interval: int = 5):
        self.poll_interval = poll_interval
        self.news_feed = deque(maxlen=100)
        self.last_id = 0
        self.is_running = False
        self._url = "https://app.cj.sina.com.cn/api/news/pc"
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://finance.sina.com.cn/7x24/"
        }
        self._client: Optional[httpx.AsyncClient] = None

    def _convert_to_chicago(self, beijing_time_str: str) -> str:
        """Converts Sina's Beijing time string to Chicago time string."""
        try:
            # Parse Sina time (naively as Beijing time)
            dt_bj = datetime.strptime(beijing_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
            # Convert to Chicago
            dt_chi = dt_bj.astimezone(CHICAGO)
            return dt_chi.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.error(f"Error converting time {beijing_time_str}: {e}")
            return beijing_time_str

    async def fetch_once(self):
        params = {
            "num": 20,
            "page": 1,
            "tag": "",
            "since_id": self.last_id,
            "_": int(datetime.now().timestamp() * 1000)
        }
        
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
            
        try:
            response = await self._client.get(self._url, params=params, headers=self._headers)
            if response.status_code == 200:
                data = response.json()
                items = data.get('result', {}).get('data', {}).get('feed', {}).get('list', [])
                
                new_items = []
                # Process items from oldest to newest to maintain temporal order in deque
                for item in reversed(items):
                    item_id = int(item.get('id', 0))
                    if item_id > self.last_id:
                        processed_item = {
                            "id": item_id,
                            "time": self._convert_to_chicago(item.get('create_time')),
                            "content": item.get('content'),
                            # We keep rich_text but won't use it in UI to avoid XSS
                            "rich_text": item.get('rich_text'),
                            "tags": [t.get('name') for t in item.get('tag', [])],
                            "received_at": datetime.now(CHICAGO).isoformat()
                        }
                        new_items.append(processed_item)
                        self.news_feed.appendleft(processed_item)
                
                if new_items:
                    self.last_id = max(item["id"] for item in new_items)
                    logger.info(f"Fetched {len(new_items)} new news items. Newest ID: {self.last_id}")
                    return new_items
            else:
                logger.warning(f"Failed to fetch news: Status {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching news: {e}")
        return []

    async def poll_loop(self):
        self.is_running = True
        logger.info("News poll loop started.")
        
        while self.is_running:
            try:
                await self.fetch_once()
            except Exception as e:
                logger.error(f"Error in news poll loop: {e}")
            
            # Use sleep to control interval
            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self.is_running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    def get_latest(self, count: int = 50) -> List[Dict]:
        return list(self.news_feed)[:count]

