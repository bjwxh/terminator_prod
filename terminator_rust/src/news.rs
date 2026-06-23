use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::collections::VecDeque;
use tokio::sync::Mutex;
use serde::{Deserialize, Serialize};
use chrono::{TimeZone, NaiveDateTime};
use chrono_tz::Asia::Shanghai;
use chrono_tz::America::Chicago;
use tracing::{info, warn, error};

#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct NewsItem {
    pub id: u64,
    pub time: String,
    pub content: String,
    pub tags: Vec<String>,
    pub received_at: String,
}

#[derive(Deserialize, Debug)]
struct SinaResponse {
    result: Option<SinaResult>,
}

#[derive(Deserialize, Debug)]
struct SinaResult {
    data: Option<SinaData>,
}

#[derive(Deserialize, Debug)]
struct SinaData {
    feed: Option<SinaFeed>,
}

#[derive(Deserialize, Debug)]
struct SinaFeed {
    list: Option<Vec<SinaNewsItem>>,
}

#[derive(Deserialize, Debug)]
struct SinaNewsItem {
    id: Option<serde_json::Value>,
    create_time: Option<String>,
    rich_text: Option<String>,
    tag: Option<Vec<SinaTag>>,
}

#[derive(Deserialize, Debug)]
struct SinaTag {
    name: Option<String>,
}

pub struct NewsFetcher {
    pub news_feed: Arc<Mutex<VecDeque<NewsItem>>>,
    pub last_id: AtomicU64,
    client: reqwest::Client,
}

impl NewsFetcher {
    pub fn new() -> Arc<Self> {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            reqwest::header::USER_AGENT,
            reqwest::header::HeaderValue::from_static("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        );
        headers.insert(
            reqwest::header::REFERER,
            reqwest::header::HeaderValue::from_static("https://finance.sina.com.cn/7x24/"),
        );

        let client = reqwest::Client::builder()
            .default_headers(headers)
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .unwrap_or_default();

        Arc::new(Self {
            news_feed: Arc::new(Mutex::new(VecDeque::with_capacity(100))),
            last_id: AtomicU64::new(0),
            client,
        })
    }

    fn convert_to_chicago(&self, beijing_time_str: &str) -> String {
        // format: "YYYY-MM-DD HH:MM:SS"
        match NaiveDateTime::parse_from_str(beijing_time_str, "%Y-%m-%d %H:%M:%S") {
            Ok(naive_dt) => {
                // Treat naive datetime as Beijing (Shanghai) time
                match Shanghai.from_local_datetime(&naive_dt) {
                    chrono::LocalResult::Single(dt_bj) => {
                        // Convert to Chicago
                        let dt_chi = dt_bj.with_timezone(&Chicago);
                        dt_chi.format("%Y-%m-%d %H:%M:%S").to_string()
                    }
                    _ => beijing_time_str.to_string(),
                }
            }
            Err(_) => beijing_time_str.to_string(),
        }
    }

    pub async fn fetch_once(&self) -> Vec<NewsItem> {
        let current_last_id = self.last_id.load(Ordering::Relaxed);
        let now_ms = chrono::Utc::now().timestamp_millis();
        
        let params = [
            ("num", "20"),
            ("page", "1"),
            ("tag", ""),
            ("since_id", &current_last_id.to_string()),
            ("_", &now_ms.to_string()),
        ];

        let url = "https://app.cj.sina.com.cn/api/news/pc";
        let response = match self.client.get(url).query(&params).send().await {
            Ok(resp) => resp,
            Err(e) => {
                warn!("Warning: Failed to fetch news (maybe timeout): {:?}", e);
                return Vec::new();
            }
        };

        if !response.status().is_success() {
            warn!("Failed to fetch news: Status {}", response.status());
            return Vec::new();
        }

        let body = match response.json::<SinaResponse>().await {
            Ok(body) => body,
            Err(e) => {
                error!("Error parsing news JSON: {:?}", e);
                return Vec::new();
            }
        };

        let list = body.result
            .and_then(|r| r.data)
            .and_then(|d| d.feed)
            .and_then(|f| f.list)
            .unwrap_or_default();

        let mut new_items = Vec::new();
        let now_chi = chrono::Local::now().with_timezone(&Chicago).to_rfc3339();

        // Process items oldest to newest (reversed from the received list)
        let mut feed = self.news_feed.lock().await;
        
        for item in list.into_iter().rev() {
            let item_id = match &item.id {
                Some(serde_json::Value::Number(num)) => num.as_u64().unwrap_or(0),
                Some(serde_json::Value::String(s)) => s.parse::<u64>().unwrap_or(0),
                _ => 0,
            };

            if item_id > current_last_id {
                let create_time = item.create_time.unwrap_or_default();
                let rich_text = item.rich_text.unwrap_or_default();
                let tags = item.tag.unwrap_or_default()
                    .into_iter()
                    .filter_map(|t| t.name)
                    .collect();

                let processed_item = NewsItem {
                    id: item_id,
                    time: self.convert_to_chicago(&create_time),
                    content: rich_text,
                    tags,
                    received_at: now_chi.clone(),
                };

                new_items.push(processed_item.clone());

                // Push front to mimic Python's appendleft
                feed.push_front(processed_item);
                if feed.len() > 100 {
                    feed.pop_back();
                }
            }
        }

        if !new_items.is_empty() {
            let newest_id = new_items.iter().map(|item| item.id).max().unwrap_or(current_last_id);
            self.last_id.store(newest_id, Ordering::Relaxed);
            info!("Fetched {} new news items. Newest ID: {}", new_items.len(), newest_id);
        }

        new_items
    }

    pub async fn run(&self) {
        info!("News poll loop started.");
        loop {
            let _ = self.fetch_once().await;
            tokio::time::sleep(std::time::Duration::from_secs(5)).await;
        }
    }

    pub async fn get_latest(&self, count: usize) -> Vec<NewsItem> {
        let feed = self.news_feed.lock().await;
        feed.iter().take(count).cloned().collect()
    }

    pub fn last_id(&self) -> u64 {
        self.last_id.load(Ordering::Relaxed)
    }
}
