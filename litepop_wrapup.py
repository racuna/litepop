#!/usr/bin/env python3
"""
Podcast Wrapped - Analyze your last year of podcast listening
Like Spotify Wrapped but for your gPodder-synced podcasts
"""

import json
import requests
import configparser
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter
import email.utils
import statistics

class PodcastWrapped:
    def __init__(self, config_path=None):
        self.config = configparser.ConfigParser()
        
        # Load config from litepop or create default
        if config_path:
            self.config.read(config_path)
        else:
            litepop_config = Path.home() / ".config" / "litepop.conf"
            if litepop_config.exists():
                self.config.read(litepop_config)
            else:
                self.create_default_config()
        
        self.session = requests.Session()
        self.session.auth = (
            self.config.get("gpodder", "username"),
            self.config.get("gpodder", "password")
        )
        
        self.server_url = self.config.get("gpodder", "server_url")
        self.device_id = self.config.get("gpodder", "device_id", fallback="default")
        self.backend = self.config.get("gpodder", "backend", fallback="opodsync")
        
        # Cache for podcast metadata
        self.podcast_cache = {}
        
    def create_default_config(self):
        """Create minimal config for standalone usage"""
        self.config["gpodder"] = {
            "server_url": input("gPodder server URL: "),
            "username": input("Username: "),
            "password": input("Password: "),
            "backend": input("Backend (opodsync/nextcloud): ") or "opodsync",
            "device_id": "default"
        }

    def get_episode_actions(self, since_date=None):
        """Fetch episode actions from the last year"""
        if not since_date:
            since_date = datetime.now() - timedelta(days=365)
        
        print(f"📊 Fetching your podcast data from the last year...")
        
        if self.backend == "nextcloud":
            url = urljoin(self.server_url, "episode_action")
        else:
            url = urljoin(self.server_url, f"api/2/episodes/{self.config.get('gpodder', 'username')}.json")
            url += f"?since={int(since_date.timestamp())}"
        
        try:
            resp = self.session.get(url, headers={"User-Agent": "podcast-wrapped/1.0"})
            resp.raise_for_status()
            
            data = resp.json()
            actions = []
            
            if isinstance(data, dict):
                actions = data.get("actions", [])
            elif isinstance(data, list):
                actions = data
                
            # Filter to last year and valid actions
            cutoff_timestamp = since_date.timestamp()
            filtered_actions = []
            
            for action in actions:
                if not isinstance(action, dict):
                    continue
                    
                timestamp = action.get("timestamp", "")
                if not timestamp:
                    continue
                    
                try:
                    # Parse timestamp
                    if 'T' in timestamp:
                        action_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    else:
                        action_date = datetime.fromtimestamp(float(timestamp))
                        
                    if action_date.timestamp() >= cutoff_timestamp:
                        filtered_actions.append(action)
                        
                except (ValueError, TypeError):
                    continue
            
            print(f"📅 Found {len(filtered_actions)} episode actions from the last year")
            return filtered_actions
            
        except Exception as e:
            print(f"❌ Error fetching episode actions: {e}")
            return []

    def get_podcast_metadata(self, feed_url):
        """Fetch podcast metadata from RSS feed"""
        if feed_url in self.podcast_cache:
            return self.podcast_cache[feed_url]
            
        try:
            resp = requests.get(feed_url, headers={"User-Agent": "podcast-wrapped/1.0"}, timeout=30)
            resp.raise_for_status()
            
            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            
            if channel is not None:
                title_elem = channel.find("title")
                title = title_elem.text.strip() if title_elem is not None and title_elem.text else "Unknown Podcast"
                
                # Try to find image
                image = None
                image_elem = channel.find("image/url")
                if image_elem is not None and image_elem.text:
                    image = image_elem.text
                else:
                    # Try iTunes image
                    itunes_image = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
                    if itunes_image is not None:
                        image = itunes_image.get("href")
                
                metadata = {
                    "title": title,
                    "image": image,
                    "feed_url": feed_url
                }
                
                self.podcast_cache[feed_url] = metadata
                return metadata
                
        except Exception as e:
            print(f"⚠️  Could not fetch metadata for {feed_url}: {e}")
            
        # Return minimal metadata
        metadata = {
            "title": urlparse(feed_url).netloc,
            "image": None,
            "feed_url": feed_url
        }
        self.podcast_cache[feed_url] = metadata
        return metadata

    def analyze_listening_patterns(self, actions):
        """Analyze listening patterns and create wrapped summary"""
        print("🔍 Analyzing your listening patterns...")
        
        # Group by podcast AND episode to avoid double-counting
        episode_stats = defaultdict(lambda: {
            "podcast": "",
            "max_position": 0,
            "total_duration": 0,
            "completed": False,
            "play_count": 0,
            "last_action": None
        })
        
        podcast_episodes = defaultdict(set)  # Track unique episodes per podcast
        all_sessions = []
        download_count = 0
        
        for action in actions:
            podcast_url = action.get("podcast", "")
            episode_url = action.get("episode", "")
            action_type = action.get("action", "").lower()
            
            if not podcast_url or not episode_url:
                continue
                
            # Get podcast metadata
            podcast_info = self.get_podcast_metadata(podcast_url)
            podcast_key = podcast_info["title"]
            
            # Create unique episode key
            episode_key = f"{podcast_url}|{episode_url}"
            
            if action_type == "download":
                download_count += 1
                # Mark episode as downloaded but don't count as listening time
                
            elif action_type == "play":
                stats = episode_stats[episode_key]
                stats["podcast"] = podcast_key
                
                # Track unique episodes per podcast
                podcast_episodes[podcast_key].add(episode_key)
                
                # Get position data with proper None handling
                position = action.get("position") or 0
                total = action.get("total") or 0
                
                try:
                    position = int(float(position)) if position is not None else 0
                    total = int(float(total)) if total is not None else 0
                except (ValueError, TypeError):
                    position = total = 0
                
                # Update max position for this episode
                if position > stats["max_position"]:
                    stats["max_position"] = position
                    
                # Update total duration
                if total > stats["total_duration"]:
                    stats["total_duration"] = total
                
                # Check completion (98% threshold)
                if total > 0 and position > 0:
                    progress = (position / total) * 100
                    if progress >= 98:
                        stats["completed"] = True
                
                stats["play_count"] += 1
                
                # Track listening session time
                if action.get("timestamp"):
                    try:
                        session_time = datetime.fromisoformat(action["timestamp"].replace('Z', '+00:00'))
                        all_sessions.append(session_time)
                        stats["last_action"] = session_time
                    except:
                        pass
        
        # Now calculate ACTUAL listening time
        podcast_summary = defaultdict(lambda: {
            "episodes_played": 0,
            "total_time_seconds": 0,
            "completed_episodes": 0,
            "unique_episodes": set()
        })
        
        total_listening_time = 0
        
        for episode_key, stats in episode_stats.items():
            if stats["max_position"] > 0:  # Only count episodes that were actually played
                podcast = stats["podcast"]
                summary = podcast_summary[podcast]
                
                summary["episodes_played"] += 1
                summary["unique_episodes"].add(episode_key)
                
                # Calculate listening time for this episode
                listening_time = stats["max_position"]  # Use max position reached
                summary["total_time_seconds"] += listening_time
                total_listening_time += listening_time
                
                if stats["completed"]:
                    summary["completed_episodes"] += 1
        
        # Convert sets to counts for JSON serialization
        for podcast, summary in podcast_summary.items():
            summary["unique_episodes"] = len(summary["unique_episodes"])
        
        print(f"📊 Found {len(episode_stats)} unique episodes")
        print(f"🎧 Calculated listening time: {total_listening_time/3600:.1f} hours")
        print(f"📥 Download actions: {download_count}")
        
        # Calculate insights
        insights = self.calculate_insights(dict(podcast_summary), all_sessions, total_listening_time)
        
        return {
            "podcast_stats": dict(podcast_summary),
            "total_listening_time": total_listening_time,
            "total_sessions": len(all_sessions),
            "unique_episodes": len(episode_stats),
            "download_count": download_count,
            "insights": insights
        }

    def calculate_insights(self, podcast_stats, all_sessions, total_time):
        """Calculate interesting insights from the data"""
        insights = {}
        
        # Top podcasts by time
        top_by_time = sorted(
            podcast_stats.items(), 
            key=lambda x: x[1]["total_time_seconds"], 
            reverse=True
        )[:10]
        insights["top_podcasts_by_time"] = top_by_time
        
        # Top podcasts by episodes played
        top_by_episodes = sorted(
            podcast_stats.items(), 
            key=lambda x: x[1]["episodes_played"], 
            reverse=True
        )[:10]
        insights["top_podcasts_by_episodes"] = top_by_episodes
        
        # Most completed podcasts (completion rate)
        completion_rates = []
        for podcast, stats in podcast_stats.items():
            if stats["episodes_played"] > 0:
                rate = (stats["completed_episodes"] / stats["episodes_played"]) * 100
                completion_rates.append((podcast, rate, stats["completed_episodes"], stats["episodes_played"]))
        
        completion_rates.sort(key=lambda x: x[1], reverse=True)
        insights["top_completion_rates"] = completion_rates[:10]
        
        # Listening patterns
        if all_sessions:
            # Most active day of week
            day_counts = Counter(session.weekday() for session in all_sessions)
            most_active_day = max(day_counts.items(), key=lambda x: x[1])
            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            insights["most_active_day"] = (day_names[most_active_day[0]], most_active_day[1])
            
            # Most active hour
            hour_counts = Counter(session.hour for session in all_sessions)
            most_active_hour = max(hour_counts.items(), key=lambda x: x[1])
            insights["most_active_hour"] = most_active_hour
            
            # Listening streaks
            insights["listening_streaks"] = self.calculate_streaks(all_sessions)
        
        # Time statistics
        hours = total_time / 3600 if total_time > 0 else 0
        insights["total_hours"] = round(hours, 1)
        insights["daily_average"] = round(hours / 365, 1) if hours > 0 else 0
        
        # Unique podcasts
        insights["unique_podcasts"] = len(podcast_stats)
        
        # Total episodes
        total_episodes = sum(stats["episodes_played"] for stats in podcast_stats.values())
        insights["total_episodes_played"] = total_episodes
        
        return insights

    def calculate_streaks(self, sessions):
        """Calculate listening streaks"""
        if not sessions:
            return []
            
        # Sort sessions by date
        sorted_sessions = sorted(sessions)
        
        streaks = []
        current_streak = 1
        streak_start = sorted_sessions[0].date()
        
        for i in range(1, len(sorted_sessions)):
            prev_date = sorted_sessions[i-1].date()
            curr_date = sorted_sessions[i].date()
            
            # Check if it's consecutive days
            if (curr_date - prev_date).days == 1:
                current_streak += 1
            elif (curr_date - prev_date).days > 1:
                # Streak broken
                if current_streak >= 2:
                    streaks.append({
                        "start": streak_start,
                        "end": prev_date,
                        "length": current_streak
                    })
                current_streak = 1
                streak_start = curr_date
        
        # Don't forget the last streak
        if current_streak >= 2:
            streaks.append({
                "start": streak_start,
                "end": sorted_sessions[-1].date(),
                "length": current_streak
            })
        
        return sorted(streaks, key=lambda x: x["length"], reverse=True)[:5]

    def generate_report(self, analysis):
        """Generate a beautiful text report"""
        insights = analysis["insights"]
        
        report = []
        report.append("🎧 YOUR PODCAST WRAPPED 🎧")
        report.append("=" * 50)
        report.append("")
        
        # Summary stats
        report.append("📊 SUMMARY")
        report.append("-" * 20)
        report.append(f"🎧 Total listening time: {insights['total_hours']} hours")
        report.append(f"📅 Daily average: {insights['daily_average']} hours")
        report.append(f"🎵 Unique podcasts: {insights['unique_podcasts']}")
        report.append(f"▶️  Total episodes played: {insights['total_episodes_played']}")
        report.append(f"📥 Total downloads: {analysis.get('download_count', 0)}")
        report.append(f"🎧 Unique episodes: {analysis.get('unique_episodes', 0)}")
        report.append("")
        
        # Handle case where there's no listening data
        if insights['total_hours'] == 0:
            report.append("❓ No listening time data available")
            report.append("This might mean:")
            report.append("- Your gPodder server doesn't track playback position")
            report.append("- You use a different app for listening")
            report.append("- Episodes are marked as 'download' but not 'play' actions")
            report.append("")
            
            # Still show episode counts
            if insights["top_podcasts_by_episodes"]:
                report.append("📊 EPISODES PLAYED")
                report.append("-" * 20)
                for i, (podcast, stats) in enumerate(insights["top_podcasts_by_episodes"][:5], 1):
                    report.append(f"{i}. {podcast} - {stats['episodes_played']} episodes")
                report.append("")
            
            return "\n".join(report)
        
        # Top podcasts by time
        report.append("⏱️  TOP PODCASTS BY LISTENING TIME")
        report.append("-" * 40)
        for i, (podcast, stats) in enumerate(insights["top_podcasts_by_time"][:5], 1):
            hours = stats["total_time_seconds"] / 3600
            report.append(f"{i}. {podcast}")
            report.append(f"   {hours:.1f} hours | {stats['episodes_played']} episodes")
        report.append("")
        
        # Top podcasts by episodes
        report.append("🔢 TOP PODCASTS BY EPISODES PLAYED")
        report.append("-" * 40)
        for i, (podcast, stats) in enumerate(insights["top_podcasts_by_episodes"][:5], 1):
            report.append(f"{i}. {podcast} - {stats['episodes_played']} episodes")
        report.append("")
        
        # Completion rates
        if insights["top_completion_rates"]:
            report.append("✅ PODCASTS YOU COMPLETE MOST")
            report.append("-" * 35)
            for i, (podcast, rate, completed, total) in enumerate(insights["top_completion_rates"][:5], 1):
                report.append(f"{i}. {podcast} - {rate:.1f}% ({completed}/{total} completed)")
            report.append("")
        
        # Listening patterns
        if "most_active_day" in insights:
            report.append("🗓️  LISTENING PATTERNS")
            report.append("-" * 25)
            day_name, count = insights["most_active_day"]
            hour, hour_count = insights["most_active_hour"]
            report.append(f"📅 Most active day: {day_name} ({count} sessions)")
            report.append(f"⏰ Most active hour: {hour}:00 ({hour_count} sessions)")
            report.append("")
        
        # Listening streaks
        if insights["listening_streaks"]:
            report.append("🔥 LISTENING STREAKS")
            report.append("-" * 20)
            for streak in insights["listening_streaks"][:3]:
                report.append(f"🔥 {streak['length']} days: {streak['start']} to {streak['end']}")
            report.append("")
        
        # Fun facts - updated to work without individual episode data
        report.append("🎯 FUN FACTS")
        report.append("-" * 15)
        
        # Find podcast with highest average completion rate
        if insights["top_completion_rates"]:
            best_complete = insights["top_completion_rates"][0]
            report.append(f"📈 Best completion rate: '{best_complete[0]}' at {best_complete[1]:.1f}%")
        
        # Find most active podcast
        if insights["top_podcasts_by_episodes"]:
            most_active = insights["top_podcasts_by_episodes"][0]
            report.append(f"🎧 Most episodes: '{most_active[0]}' with {most_active[1]['episodes_played']} episodes")
        
        # Find longest listening podcast
        if insights["top_podcasts_by_time"]:
            longest = insights["top_podcasts_by_time"][0]
            hours = longest[1]["total_time_seconds"] / 3600
            report.append(f"⏱️  Most time: '{longest[0]}' with {hours:.1f} hours")
        
        return "\n".join(report)

    def save_detailed_data(self, analysis, filename="podcast_wrapped.json"):
        """Save detailed data as JSON for further analysis"""
        with open(filename, 'w') as f:
            json.dump(analysis, f, indent=2, default=str)
        print(f"💾 Detailed data saved to {filename}")

    def run(self):
        """Run the complete analysis"""
        print("🎧 Welcome to Podcast Wrapped!")
        print("Analyzing your last year of podcast listening...\n")
        
        # Get data
        actions = self.get_episode_actions()
        
        if not actions:
            print("❌ No podcast data found for the last year.")
            return
        
        # Analyze
        analysis = self.analyze_listening_patterns(actions)
        
        # Generate report
        report = self.generate_report(analysis)
        
        # Display
        print("\n" + report)
        
        # Save detailed data
        self.save_detailed_data(analysis)
        
        # Save report
        with open("podcast_wrapped_report.txt", 'w') as f:
            f.write(report)
        print("📝 Report saved to podcast_wrapped_report.txt")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate your podcast listening summary")
    parser.add_argument("--config", help="Path to litepop config file")
    parser.add_argument("--days", type=int, default=365, help="Number of days to analyze (default: 365)")
    
    args = parser.parse_args()
    
    wrapped = PodcastWrapped(config_path=args.config)
    wrapped.run()