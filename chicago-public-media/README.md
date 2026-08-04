# Chicago Public Media

Tools for collecting and analyzing Chicago Sun-Times RSS feed content.

## Python Scripts

### `rss_parser.py`

Fetches the Chicago Sun-Times RSS feed and saves entries to JSON files organized by date.

```bash
python rss_parser.py
```

Entries are saved under `data/YYYY/YYYY-MM-DD/YYYY-MM-DD.json`. Each daily file accumulates entries over time — re-running the script merges new entries without duplicating existing ones.

**Dependencies:** `feedparser`

---

### `find_niche_topics.py`

Clusters all collected articles using TF-IDF vectorization and K-Means to surface niche or underreported topic groups. Results are printed to the terminal (sorted smallest-first, so niche topics appear at the top) and written to `topic_clusters.json`.

```bash
python find_niche_topics.py
# Specify a custom number of clusters (default: 30)
python find_niche_topics.py --clusters 20
```

**Dependencies:** `scikit-learn`, `numpy`

---

### `export_cluster.py`

Exports stories from a single topic cluster to a standalone JSON file. Use the cluster IDs shown in `find_niche_topics.py` output to pick the cluster you want.

```bash
python export_cluster.py <cluster_id>
# Example: export cluster 5 to cluster_5.json
python export_cluster.py 5

# Optional: specify custom input or output paths
python export_cluster.py 5 --input topic_clusters.json --output my_cluster.json
```

---

### `find_immigration_stories.py`

Searches all collected daily JSON files for articles mentioning "immigration" in the title or summary and writes the deduplicated results to `immigration.json`, sorted newest-first.

```bash
python find_immigration_stories.py
```

---

## Data Files

| File | Description |
|------|-------------|
| `data/` | Raw RSS feed archives organized as `data/YYYY/YYYY-MM-DD/YYYY-MM-DD.json` |
| `topic_clusters.json` | Output of `find_niche_topics.py` — all clusters with top terms and stories |
| `cluster_0.json` | Example exported cluster (from `export_cluster.py`) |
| `immigration.json` | Stories matching "immigration" keyword |
| `bears.json` | Stories related to the Chicago Bears |
| `housing.json` | Stories related to housing |
| `stadiums.json` | Stories related to stadiums |

## Setup

Install dependencies using the project's virtual environment:

```bash
pip install feedparser scikit-learn numpy
```

## Typical Workflow

1. Run `rss_parser.py` (on a schedule or manually) to collect articles into `data/`.
2. Run `find_niche_topics.py` to cluster articles and identify topic groups.
3. Use `export_cluster.py` with a cluster ID to extract stories for deeper analysis.
4. Run topic-specific scripts (e.g., `find_immigration_stories.py`) to filter by keyword.
