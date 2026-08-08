#!/usr/bin/env python3
"""
RSS Feed Parser for Chicago Sun-Times
Parses RSS feed and saves entries to JSON files organized by year and date.
"""

import feedparser
import json
from pathlib import Path
from datetime import datetime
import time


def parse_feed(feed_url):
    """Parse RSS feed and return entries."""
    print(f"Fetching feed from {feed_url}...")
    feed = feedparser.parse(feed_url)
    
    if feed.bozo:
        print(f"Warning: Feed parsing had issues: {feed.bozo_exception}")
    
    print(f"Feed title: {feed.feed.get('title', 'Unknown')}")
    print(f"Found {len(feed.entries)} entries")
    
    return feed


def get_entry_date(entry):
    """Extract and parse the published date from an entry."""
    # Try published_parsed first, then updated_parsed
    date_tuple = entry.get('published_parsed') or entry.get('updated_parsed')
    
    if date_tuple:
        return datetime(*date_tuple[:6])
    
    # Fallback to current date if no date found
    return datetime.now()


def save_entries_to_json(feed, base_dir='data'):
    """Save feed entries to a single JSON file per day, organized by year and date."""
    base_path = Path(base_dir)
    
    # Group entries by date
    entries_by_date = {}
    for entry in feed.entries:
        entry_date = get_entry_date(entry)
        date_key = entry_date.strftime('%Y-%m-%d')
        
        if date_key not in entries_by_date:
            entries_by_date[date_key] = []
        
        # Prepare entry data
        entry_data = {
            'title': entry.get('title', ''),
            'link': entry.get('link', ''),
            'published': entry.get('published', ''),
            'published_parsed': entry_date.isoformat() if entry_date else None,
            'summary': entry.get('summary', ''),
            'author': entry.get('author', ''),
            'id': entry.get('id', ''),
            'tags': [tag.get('term', '') for tag in entry.get('tags', [])],
        }
        
        entries_by_date[date_key].append(entry_data)
    
    # Save one file per date
    for date_key, new_entries in entries_by_date.items():
        entry_date = datetime.strptime(date_key, '%Y-%m-%d')
        
        # Create directory structure: data/yyyy/yyyy-mm-dd/
        year_dir = base_path / str(entry_date.year)
        date_dir = year_dir / date_key
        date_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename with date
        filename = f"{date_key}.json"
        filepath = date_dir / filename
        
        # Load existing entries if file exists
        existing_entries = []
        existing_ids = set()
        if filepath.exists():
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
                    existing_entries = existing_data.get('entries', [])
                    # Track existing entry IDs to avoid duplicates
                    existing_ids = {entry.get('id') or entry.get('link') 
                                   for entry in existing_entries}
                print(f"Found {len(existing_entries)} existing entries in {filepath}")
            except json.JSONDecodeError:
                print(f"Warning: Could not parse existing file {filepath}, will overwrite")
        
        # Merge new entries, avoiding duplicates
        added_count = 0
        for entry in new_entries:
            entry_id = entry.get('id') or entry.get('link')
            if entry_id not in existing_ids:
                existing_entries.append(entry)
                existing_ids.add(entry_id)
                added_count += 1
        
        # Prepare daily data
        daily_data = {
            'date': date_key,
            'entry_count': len(existing_entries),
            'entries': existing_entries,
            'last_updated': datetime.now().isoformat()
        }
        
        # Save to JSON
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(daily_data, f, indent=2, ensure_ascii=False)
        
        if added_count > 0:
            print(f"Added {added_count} new entries to {filepath} (total: {len(existing_entries)})")
        else:
            print(f"No new entries to add to {filepath} (total: {len(existing_entries)})")
    
    print(f"\nAll entries saved to {base_path}")


def save_full_feed(feed, base_dir='data'):
    """Save the complete feed data to a single JSON file."""
    base_path = Path(base_dir)
    now = datetime.now()
    
    # Create directory structure
    year_dir = base_path / str(now.year)
    date_dir = year_dir / now.strftime('%Y-%m-%d')
    date_dir.mkdir(parents=True, exist_ok=True)
    
    # Create filename with timestamp
    timestamp = now.strftime('%Y%m%d_%H%M%S')
    filename = f"full_feed_{timestamp}.json"
    filepath = date_dir / filename
    
    # Prepare feed data
    feed_data = {
        'feed_info': {
            'title': feed.feed.get('title', ''),
            'link': feed.feed.get('link', ''),
            'description': feed.feed.get('description', ''),
            'updated': feed.feed.get('updated', ''),
        },
        'entries': []
    }
    
    for entry in feed.entries:
        entry_date = get_entry_date(entry)
        entry_data = {
            'title': entry.get('title', ''),
            'link': entry.get('link', ''),
            'published': entry.get('published', ''),
            'published_parsed': entry_date.isoformat() if entry_date else None,
            'summary': entry.get('summary', ''),
            'author': entry.get('author', ''),
            'id': entry.get('id', ''),
            'tags': [tag.get('term', '') for tag in entry.get('tags', [])],
        }
        feed_data['entries'].append(entry_data)
    
    # Save to JSON
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(feed_data, f, indent=2, ensure_ascii=False)
    
    print(f"Full feed saved to: {filepath}")


def main():
    """Main function to run the RSS parser."""
    feed_url = "https://chicago.suntimes.com/rss/index.xml"
    
    # Parse the feed
    feed = parse_feed(feed_url)
    
    # Save entries (choose one or both methods)
    # Method 1: Save each entry as a separate file
    save_entries_to_json(feed)
    
    # Method 2: Save complete feed as a single file
    # save_full_feed(feed)


if __name__ == "__main__":
    main()
