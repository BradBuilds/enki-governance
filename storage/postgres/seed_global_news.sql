-- Seed Global News Connectors
-- Run: docker compose exec -T postgres psql -U enki_db -d enki < storage/postgres/seed_global_news.sql

-- source_config is bytea — we store JSON as bytes
-- device_id must be unique per connector
-- source_type: 'rss' for RSS feeds

INSERT INTO connectors (connector_id, device_id, tenant_id, connector_type, source_type, source_config, poll_interval, status) VALUES
-- World News
('conn_reuters_world', 'dev_reuters_world', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.reuters.com/Reuters/worldNews", "category": "world", "region": "global", "label": "Reuters World"}', 'UTF8'), '15 minutes', 'active'),
('conn_bbc_world', 'dev_bbc_world', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "world", "region": "global", "label": "BBC World"}', 'UTF8'), '15 minutes', 'active'),
('conn_aljazeera', 'dev_aljazeera', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "world", "region": "middle_east", "label": "Al Jazeera"}', 'UTF8'), '15 minutes', 'active'),
('conn_dw_world', 'dev_dw_world', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://rss.dw.com/rdf/rss-en-world", "category": "world", "region": "europe", "label": "DW News"}', 'UTF8'), '15 minutes', 'active'),
('conn_france24', 'dev_france24', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.france24.com/en/rss", "category": "world", "region": "europe", "label": "France 24"}', 'UTF8'), '15 minutes', 'active'),

-- Politics
('conn_reuters_politics', 'dev_reuters_politics', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.reuters.com/Reuters/PoliticsNews", "category": "politics", "region": "global", "label": "Reuters Politics"}', 'UTF8'), '15 minutes', 'active'),
('conn_bbc_politics', 'dev_bbc_politics', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.bbci.co.uk/news/politics/rss.xml", "category": "politics", "region": "europe", "label": "BBC Politics"}', 'UTF8'), '30 minutes', 'active'),

-- Defense & Security
('conn_defense_one', 'dev_defense_one', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.defenseone.com/rss/", "category": "defense", "region": "north_america", "label": "Defense One"}', 'UTF8'), '30 minutes', 'active'),
('conn_warzone', 'dev_warzone', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.thedrive.com/the-war-zone/feed", "category": "defense", "region": "global", "label": "The War Zone"}', 'UTF8'), '30 minutes', 'active'),

-- Technology
('conn_reuters_tech', 'dev_reuters_tech', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.reuters.com/reuters/technologyNews", "category": "technology", "region": "global", "label": "Reuters Tech"}', 'UTF8'), '15 minutes', 'active'),
('conn_arstechnica', 'dev_arstechnica', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.arstechnica.com/arstechnica/index", "category": "technology", "region": "north_america", "label": "Ars Technica"}', 'UTF8'), '30 minutes', 'active'),
('conn_hn_top', 'dev_hn_top', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://hnrss.org/frontpage", "category": "technology", "region": "global", "label": "Hacker News"}', 'UTF8'), '15 minutes', 'active'),

-- Economy
('conn_reuters_business', 'dev_reuters_business', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.reuters.com/reuters/businessNews", "category": "economy", "region": "global", "label": "Reuters Business"}', 'UTF8'), '15 minutes', 'active'),

-- Science
('conn_bbc_science', 'dev_bbc_science', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "category": "science", "region": "global", "label": "BBC Science"}', 'UTF8'), '30 minutes', 'active'),

-- Middle East
('conn_middleeasteye', 'dev_middleeasteye', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.middleeasteye.net/rss", "category": "world", "region": "middle_east", "label": "Middle East Eye"}', 'UTF8'), '15 minutes', 'active'),
('conn_timesofisrael', 'dev_timesofisrael', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.timesofisrael.com/feed/", "category": "world", "region": "middle_east", "label": "Times of Israel"}', 'UTF8'), '15 minutes', 'active'),

-- Asia
('conn_scmp', 'dev_scmp', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.scmp.com/rss/91/feed", "category": "world", "region": "asia", "label": "SCMP"}', 'UTF8'), '15 minutes', 'active'),

-- Canada
('conn_cbc', 'dev_cbc', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://rss.cbc.ca/lineup/topstories.xml", "category": "world", "region": "north_america", "label": "CBC News"}', 'UTF8'), '15 minutes', 'active'),

-- Africa
('conn_africanews', 'dev_africanews', 'default', 'pull', 'rss_feed', convert_to('{"url": "https://www.africanews.com/feed/", "category": "world", "region": "africa", "label": "Africa News"}', 'UTF8'), '30 minutes', 'active')

ON CONFLICT (connector_id) DO UPDATE SET status = 'active', updated_at = NOW();

SELECT count(*) as total_connectors, status FROM connectors GROUP BY status;
