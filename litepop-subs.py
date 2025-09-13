#!/usr/bin/env python3
"""
litepop-subs - Subscription Manager for litepop
Manages podcast subscriptions with opodsync/nextcloud-gpodder synchronization
"""

import curses
import json
import os
import time
import threading
import requests
import configparser
import xml.etree.ElementTree as ET
import email.utils
import locale
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional, Tuple
from pathlib import Path

def log(msg: str) -> None:
    """Global logging function"""
    with open("/tmp/litepop_subs_debug.log", "a") as f:
        f.write(f"{datetime.now()}: {msg}\n")

def clean_text_for_display(text: str) -> str:
    """Clean text for safe display in curses"""
    if not isinstance(text, str):
        text = str(text)
    
    # Replace problematic characters
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)  # Remove control chars
    text = re.sub(r'[^\x20-\x7E\u00A0-\uFFFF]', '?', text)        # Replace invalid chars
    text = text.replace('\r', ' ').replace('\n', ' ')               # Replace newlines
    text = re.sub(r'\s+', ' ', text)                               # Normalize spaces
    
    return text.strip()

class Config:
    """Handles configuration file operations"""
    def __init__(self):
        self.config_file = Path.home() / ".config" / "litepop.conf"
        self.config = configparser.ConfigParser()
        self.load_config()

    def load_config(self) -> None:
        if self.config_file.exists():
            self.config.read(self.config_file)
        else:
            self.create_default_config()

    def create_default_config(self) -> None:
        """Creates default configuration if none exists"""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config["gpodder"] = {
            "server_url": "https://your-opodsync-server.com",
            "username": "",
            "password": "",
            "sync_interval": "300",
            "backend": "opodsync",  # nextcloud or opodsync
            "device_id": "default"
        }
        self.config["player"] = {
            "temp_dir": "/tmp/litepop",
            "default_speed": "1.0",
            "player_command": "mpv --no-config --no-video --af=loudnorm=i=-16:lra=11:tp=-1.5 --speed={speed} --start={start_time} --input-ipc-server={ipc_socket} {file}"
        }
        self.save_config()

    def save_config(self) -> None:
        with self.config_file.open("w") as f:
            self.config.write(f)

    def get(self, section: str, key: str, fallback: Optional[str] = None) -> str:
        return self.config.get(section, key, fallback=fallback)

    def set(self, section: str, key: str, value: str) -> None:
        if section not in self.config:
            self.config[section] = {}
        self.config[section][key] = value
        self.save_config()

class GPodderSync:
    def __init__(self, config: Config):
        self.server_url = config.get("gpodder", "server_url")
        self.username = config.get("gpodder", "username")
        self.password = config.get("gpodder", "password")
        self.device_id = config.get("gpodder", "device_id")
        self.backend = config.get("gpodder", "backend", "opodsync").lower()

        self.session = requests.Session()
        self.session.auth = (self.username, self.password)
        log(f"Initialized {self.backend} backend with URL: {self.server_url}")

        # Resolve device_id "default" for opodsync
        try:
            self._resolve_device_id()
        except Exception as e:
            log(f"Could not resolve device_id: {str(e)}")

    def _resolve_device_id(self) -> None:
        """If device_id is 'default', get device list from server and use the first one"""
        try:
            if self.device_id != "default":
                return
            
            if self.backend == "opodsync":
                url = urljoin(self.server_url, f"api/2/devices/{self.username}.json")
            else:
                # For nextcloud, we don't need to resolve device_id
                return
                
            resp = self.session.get(url, headers={"User-Agent": "litepop-subs/1.0"}, timeout=15)
            if resp.ok and resp.content:
                data = resp.json()
                if isinstance(data, list) and data:
                    first = data[0]
                    if isinstance(first, dict) and "id" in first:
                        self.device_id = first["id"]
                        log(f"Resolved device_id 'default' -> '{self.device_id}'")
        except Exception as e:
            log(f"Could not resolve device id: {str(e)}")

    def get_subscriptions(self) -> List[str]:
        """Get subscriptions from server"""
        try:
            if self.backend == "nextcloud":
                url = urljoin(self.server_url, "subscription")
            elif self.backend == "opodsync":
                url = urljoin(self.server_url, f"subscriptions/{self.username}/{self.device_id}.json")
            else:
                raise ValueError(f"Unknown backend: {self.backend}")

            log(f"Fetching subscriptions from: {url}")
            resp = self.session.get(url, headers={"User-Agent": "litepop-subs/1.0"}, timeout=30)
            resp.raise_for_status()
            
            if not resp.content:
                log("Empty response from subscriptions endpoint")
                return []
            
            data = resp.json()
            log(f"Raw subscriptions response: {data}")

            # Handle different response formats
            if isinstance(data, dict):
                if "add" in data:
                    subscriptions = data.get("add", [])
                elif "subscriptions" in data:
                    subscriptions = data.get("subscriptions", [])
                elif "data" in data:
                    subscriptions = [v for v in data.get("data", []) if isinstance(v, str)]
                else:
                    subscriptions = []
            elif isinstance(data, list):
                subscriptions = data
            else:
                subscriptions = []

            log(f"Retrieved {len(subscriptions)} subscriptions")
            return subscriptions
            
        except Exception as e:
            log(f"Error retrieving subscriptions: {str(e)}")
            return []

    def upload_subscription_changes(self, add_urls: List[str] = None, remove_urls: List[str] = None) -> bool:
        """Upload subscription changes to server"""
        try:
            add_urls = add_urls or []
            remove_urls = remove_urls or []
            
            if not add_urls and not remove_urls:
                return True

            if self.backend == "nextcloud":
                # For Nextcloud, we need to handle add/remove separately
                if add_urls:
                    for url in add_urls:
                        add_url = urljoin(self.server_url, "subscription")
                        resp = self.session.post(
                            add_url,
                            headers={"Content-Type": "application/json"},
                            json={"url": url}
                        )
                        resp.raise_for_status()
                
                if remove_urls:
                    for url in remove_urls:
                        remove_url = urljoin(self.server_url, f"subscription")
                        resp = self.session.delete(
                            remove_url,
                            headers={"Content-Type": "application/json"},
                            json={"url": url}
                        )
                        resp.raise_for_status()
                        
            elif self.backend == "opodsync":
                url = urljoin(self.server_url, f"api/2/subscriptions/{self.username}/{self.device_id}.json")
                data = {}
                if add_urls:
                    data["add"] = add_urls
                if remove_urls:
                    data["remove"] = remove_urls
                
                log(f"Uploading subscription changes: {data}")
                resp = self.session.post(
                    url,
                    headers={"Content-Type": "application/json", "User-Agent": "litepop-subs/1.0"},
                    json=data,
                    timeout=30
                )
                resp.raise_for_status()
            
            log(f"Successfully uploaded subscription changes: +{len(add_urls)} -{len(remove_urls)}")
            return True
            
        except Exception as e:
            log(f"Error uploading subscription changes: {str(e)}")
            return False

class PodcastInfo:
    """Represents podcast information with metadata"""
    def __init__(self, url: str):
        self.url = url
        self.title = "Unknown Podcast"
        self.description = ""
        self.last_episode_date = None
        self.episode_count = 0
        self.author = ""
        self.website = ""
        self.category = ""
        self.image_url = ""
        self.loading = False
        self.load_error = None

    def fetch_info(self) -> bool:
        """Fetches podcast information from RSS feed"""
        self.loading = True
        try:
            log(f"Fetching podcast info: {self.url}")
            response = requests.get(
                self.url, 
                headers={"User-Agent": "litepop-subs/1.0"}, 
                timeout=15
            )
            response.raise_for_status()
            
            root = ET.fromstring(response.content)
            channel = root.find("channel")
            
            if channel is None:
                self.load_error = "Invalid RSS feed format"
                return False

            # Extract basic info with cleaning
            title_elem = channel.find("title")
            if title_elem is not None and title_elem.text:
                self.title = clean_text_for_display(title_elem.text)

            desc_elem = channel.find("description")
            if desc_elem is not None and desc_elem.text:
                self.description = clean_text_for_display(desc_elem.text)

            # Extract author/creator
            author_elem = channel.find("author") or channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}author")
            if author_elem is not None and author_elem.text:
                self.author = clean_text_for_display(author_elem.text)

            # Extract website
            link_elem = channel.find("link")
            if link_elem is not None and link_elem.text:
                self.website = clean_text_for_display(link_elem.text)

            # Extract category
            category_elem = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}category")
            if category_elem is not None:
                self.category = category_elem.get("text", "")

            # Extract image
            image_elem = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
            if image_elem is not None:
                self.image_url = image_elem.get("href", "")
            else:
                # Fallback to standard RSS image
                image_elem = channel.find("image")
                if image_elem is not None:
                    url_elem = image_elem.find("url")
                    if url_elem is not None and url_elem.text:
                        self.image_url = url_elem.text.strip()

            # Find episodes and get latest date
            episodes = root.findall(".//item")
            self.episode_count = len(episodes)
            
            latest_date = None
            for item in episodes:
                pub_date_elem = item.find("pubDate")
                if pub_date_elem is not None and pub_date_elem.text:
                    try:
                        episode_date = email.utils.parsedate_to_datetime(pub_date_elem.text)
                        if latest_date is None or episode_date > latest_date:
                            latest_date = episode_date
                    except Exception:
                        continue
            
            self.last_episode_date = latest_date
            log(f"Loaded podcast info: {self.title} ({self.episode_count} episodes)")
            return True
            
        except Exception as e:
            self.load_error = str(e)
            log(f"Error loading podcast info for {self.url}: {str(e)}")
            return False
        finally:
            self.loading = False

class SubscriptionManager:
    """Main subscription management application"""
    def __init__(self):
        self.config = Config()
        Path("/tmp/litepop_subs_debug.log").write_text("")
        log("Starting Subscription Manager")
        
        self.gpodder = GPodderSync(self.config)
        self.podcasts = []
        self.running = True
        self.stdscr = None
        self.status_message = None
        self.status_timeout = None
        self.loading_subscriptions = False
        self.ui_lock = threading.Lock()

    def init_curses(self) -> None:
        """Initializes curses for terminal UI"""
        # Set locale for proper Unicode handling
        try:
            locale.setlocale(locale.LC_ALL, 'C')  # Use C locale to avoid Unicode issues
        except:
            pass
        
        self.stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.stdscr.keypad(True)
        curses.curs_set(0)
        
        if curses.has_colors():
            curses.start_color()
            # Color pairs
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # Header
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)   # Selected
            curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)   # Success
            curses.init_pair(4, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # Warning
            curses.init_pair(5, curses.COLOR_RED, curses.COLOR_BLACK)     # Error
            curses.init_pair(6, curses.COLOR_CYAN, curses.COLOR_BLACK)    # Info
            curses.init_pair(7, curses.COLOR_MAGENTA, curses.COLOR_BLACK) # Loading

    def cleanup_curses(self) -> None:
        """Cleans up curses settings"""
        if self.stdscr:
            curses.nocbreak()
            self.stdscr.keypad(False)
            curses.echo()
            curses.endwin()

    def safe_addstr(self, window, y: int, x: int, text: str, max_width: Optional[int] = None) -> bool:
        """Safely adds string to curses window using only ASCII"""
        try:
            # Get window dimensions to prevent out-of-bounds
            max_y, max_x = window.getmaxyx()
            if y < 0 or y >= max_y or x < 0 or x >= max_x:
                return False
            
            # Convert to ASCII-only
            ascii_text = text.encode('ascii', errors='replace').decode('ascii')
            
            # Apply width limit
            if max_width:
                available_width = min(max_width, max_x - x - 1)
                ascii_text = ascii_text[:available_width]
            else:
                available_width = max_x - x - 1
                ascii_text = ascii_text[:available_width]
            
            # Add the string
            window.addstr(y, x, ascii_text)
            return True
            
        except Exception as e:
            log(f"Error in safe_addstr: {str(e)}")
            try:
                window.addstr(y, x, "<ERR>")
                return True
            except:
                return False

    def set_status_message(self, message: str, timeout: int = 3) -> None:
        """Sets temporary status message"""
        self.status_message = message
        self.status_timeout = time.time() + timeout

    def load_subscriptions(self) -> None:
        """Loads subscriptions from server"""
        def load_worker():
            self.loading_subscriptions = True
            try:
                subscription_urls = self.gpodder.get_subscriptions()
                new_podcasts = []
                
                # Create podcast objects
                for url in subscription_urls:
                    podcast = PodcastInfo(url)
                    new_podcasts.append(podcast)
                
                # Load info in parallel
                threads = []
                for podcast in new_podcasts:
                    thread = threading.Thread(target=podcast.fetch_info)
                    thread.daemon = True
                    threads.append(thread)
                    thread.start()
                
                # Wait for all to complete
                for thread in threads:
                    thread.join()
                
                # Sort by title
                new_podcasts.sort(key=lambda p: p.title.lower())
                
                with self.ui_lock:
                    self.podcasts = new_podcasts
                    
                log(f"Loaded {len(self.podcasts)} subscriptions")
                self.set_status_message(f"Loaded {len(self.podcasts)} subscriptions")
                
            except Exception as e:
                log(f"Error loading subscriptions: {str(e)}")
                self.set_status_message(f"Error loading subscriptions: {str(e)}")
            finally:
                self.loading_subscriptions = False
        
        thread = threading.Thread(target=load_worker)
        thread.daemon = True
        thread.start()

    def add_subscription(self) -> None:
        """Interactive subscription addition"""
        curses.curs_set(1)
        curses.echo()
        
        height, width = self.stdscr.getmaxyx()
        
        # Create input window
        input_win = curses.newwin(3, width - 4, height // 2 - 2, 2)
        input_win.box()
        self.safe_addstr(input_win, 1, 2, "Enter RSS/Podcast URL: ")
        input_win.refresh()
        
        # Get URL input
        url = input_win.getstr(1, 24, width - 30).decode('utf-8').strip()
        
        curses.noecho()
        curses.curs_set(0)
        del input_win
        
        if not url:
            self.set_status_message("No URL entered")
            return
        
        # Validate URL format
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            self.set_status_message("Invalid URL format")
            return
        
        # Check if already subscribed
        if any(p.url == url for p in self.podcasts):
            self.set_status_message("Already subscribed to this podcast")
            return
        
        def add_worker():
            # Test feed first
            test_podcast = PodcastInfo(url)
            if not test_podcast.fetch_info():
                self.set_status_message(f"Error: {test_podcast.load_error or 'Invalid feed'}")
                return
            
            # Add to server
            if self.gpodder.upload_subscription_changes(add_urls=[url]):
                with self.ui_lock:
                    self.podcasts.append(test_podcast)
                    self.podcasts.sort(key=lambda p: p.title.lower())
                self.set_status_message(f"Added: {test_podcast.title}")
                log(f"Added subscription: {test_podcast.title} ({url})")
            else:
                self.set_status_message("Failed to add subscription to server")
        
        self.set_status_message(f"Adding subscription...")
        thread = threading.Thread(target=add_worker)
        thread.daemon = True
        thread.start()

    def remove_subscription(self, index: int) -> None:
        """Removes subscription at given index"""
        if 0 <= index < len(self.podcasts):
            podcast = self.podcasts[index]
            
            def remove_worker():
                if self.gpodder.upload_subscription_changes(remove_urls=[podcast.url]):
                    with self.ui_lock:
                        self.podcasts.pop(index)
                    self.set_status_message(f"Removed: {podcast.title}")
                    log(f"Removed subscription: {podcast.title}")
                else:
                    self.set_status_message("Failed to remove subscription from server")
            
            self.set_status_message("Removing subscription...")
            thread = threading.Thread(target=remove_worker)
            thread.daemon = True
            thread.start()

    def format_last_update(self, last_date: Optional[datetime]) -> str:
        """Formats last episode date for display"""
        if not last_date:
            return "Unknown"
        
        now = datetime.now(last_date.tzinfo) if last_date.tzinfo else datetime.now()
        diff = now - last_date
        
        if diff.days == 0:
            return "Today"
        elif diff.days == 1:
            return "Yesterday"
        elif diff.days < 7:
            return f"{diff.days} days ago"
        elif diff.days < 30:
            weeks = diff.days // 7
            return f"{weeks} week{'s' if weeks > 1 else ''} ago"
        elif diff.days < 365:
            months = diff.days // 30
            return f"{months} month{'s' if months > 1 else ''} ago"
        else:
            years = diff.days // 365
            return f"{years} year{'s' if years > 1 else ''} ago"

    def draw_main_screen(self, selected_index: int = 0) -> None:
        """Draws the main subscription list screen"""
        with self.ui_lock:
            try:
                self.stdscr.clear()
                height, width = self.stdscr.getmaxyx()
                
                # Header
                backend_name = self.gpodder.backend.upper()
                header = f" litepop - Subscription Manager ({backend_name}) "
                self.stdscr.attron(curses.color_pair(1))
                self.safe_addstr(self.stdscr, 0, 0, header.center(width), width)
                self.stdscr.attroff(curses.color_pair(1))
                
                # Backend info
                backend_info = f"Server: {self.gpodder.server_url[:30]}... | User: {self.gpodder.username} | Device: {self.gpodder.device_id}"
                self.safe_addstr(self.stdscr, 2, 2, backend_info, width-4)
                
                # Loading message
                if self.loading_subscriptions:
                    self.stdscr.attron(curses.color_pair(7))
                    self.safe_addstr(self.stdscr, 4, 2, "Loading subscriptions, please wait...")
                    self.stdscr.attroff(curses.color_pair(7))
                
                start_row = 6
                visible_items = height - 12
                
                if not self.podcasts and not self.loading_subscriptions:
                    self.safe_addstr(self.stdscr, start_row, 2, "No subscriptions found. Press 'a' to add one.")
                else:
                    # Column headers
                    self.stdscr.attron(curses.A_BOLD)
                    header_line = f"{'Podcast Title':<40} {'Last Update':<15} {'Episodes':<10} {'Status'}"
                    self.safe_addstr(self.stdscr, start_row - 1, 2, header_line, width-4)
                    self.stdscr.attroff(curses.A_BOLD)
                    
                    # Calculate display window
                    if selected_index >= len(self.podcasts):
                        selected_index = max(0, len(self.podcasts) - 1)
                    
                    display_start = max(0, selected_index - visible_items // 2)
                    display_end = min(len(self.podcasts), display_start + visible_items)
                    
                    # Podcast list
                    for i in range(display_start, display_end):
                        podcast = self.podcasts[i]
                        row = start_row + (i - display_start)
                        
                        if row >= height - 6:  # Prevent drawing outside screen
                            break
                        
                        # Truncate title safely
                        title = podcast.title[:38] + ".." if len(podcast.title) > 40 else podcast.title
                        last_update = self.format_last_update(podcast.last_episode_date)
                        episode_count = str(podcast.episode_count) if podcast.episode_count > 0 else "?"
                        
                        # Status
                        if podcast.loading:
                            status = "Loading..."
                            color = 7  # Magenta
                        elif podcast.load_error:
                            status = "Error"
                            color = 5  # Red
                        else:
                            status = "OK"
                            color = 3  # Green
                        
                        line = f"{title:<40} {last_update:<15} {episode_count:<10} {status}"
                        
                        if i == selected_index:
                            self.stdscr.attron(curses.color_pair(2))
                        elif color:
                            self.stdscr.attron(curses.color_pair(color))
                        
                        self.safe_addstr(self.stdscr, row, 2, line, width-4)
                        
                        if i == selected_index:
                            self.stdscr.attroff(curses.color_pair(2))
                        elif color:
                            self.stdscr.attroff(curses.color_pair(color))
                    
                    # Show selection info
                    if 0 <= selected_index < len(self.podcasts):
                        podcast = self.podcasts[selected_index]
                        info_row = start_row + visible_items + 1
                        
                        if info_row < height - 4:
                            self.stdscr.attron(curses.A_BOLD)
                            self.safe_addstr(self.stdscr, info_row, 2, "Selected Podcast:")
                            self.stdscr.attroff(curses.A_BOLD)
                            
                            if info_row + 1 < height - 3:
                                url_display = podcast.url[:width-10] + "..." if len(podcast.url) > width-10 else podcast.url
                                self.safe_addstr(self.stdscr, info_row + 1, 2, f"URL: {url_display}", width-4)
                
                # Help text
                help_line = "a:Add | d:Delete | r:Refresh | ENTER:Details | q/ESC:Quit"
                if height >= 3:
                    self.safe_addstr(self.stdscr, height - 3, 2, help_line, width-4)
                
                # Status message
                if self.status_message and time.time() < self.status_timeout:
                    self.stdscr.attron(curses.color_pair(5))
                    self.safe_addstr(self.stdscr, height - 2, 2, self.status_message, width-4)
                    self.stdscr.attroff(curses.color_pair(5))
                
                self.stdscr.refresh()
            
            except Exception as e:
                log(f"Error in draw_main_screen: {str(e)}")
                try:
                    self.stdscr.clear()
                    self.safe_addstr(self.stdscr, 0, 0, f"Display error - press 'r' to refresh or 'q' to quit")
                    self.safe_addstr(self.stdscr, 1, 0, f"Error: {str(e)[:60]}")
                    self.stdscr.refresh()
                except:
                    pass

    def show_podcast_details(self, index: int) -> None:
        """Shows detailed information about a podcast"""
        if not (0 <= index < len(self.podcasts)):
            return
        
        podcast = self.podcasts[index]
        
        try:
            self.stdscr.clear()
            height, width = self.stdscr.getmaxyx()
            
            # Header
            self.stdscr.attron(curses.color_pair(1))
            header = f" Podcast Details "
            self.safe_addstr(self.stdscr, 0, 0, header.center(width), width)
            self.stdscr.attroff(curses.color_pair(1))
            
            row = 2
            
            # Basic info
            self.safe_addstr(self.stdscr, row, 2, f"Title: {podcast.title}", width-4)
            row += 2
            
            self.safe_addstr(self.stdscr, row, 2, f"URL: {podcast.url}", width-4)
            row += 2
            
            if podcast.author and row < height - 4:
                self.safe_addstr(self.stdscr, row, 2, f"Author: {podcast.author}", width-4)
                row += 1
            
            if podcast.website and row < height - 4:
                self.safe_addstr(self.stdscr, row, 2, f"Website: {podcast.website}", width-4)
                row += 1
            
            if podcast.category and row < height - 4:
                self.safe_addstr(self.stdscr, row, 2, f"Category: {podcast.category}", width-4)
                row += 1
            
            if row < height - 4:
                row += 1
                self.safe_addstr(self.stdscr, row, 2, f"Episodes: {podcast.episode_count}", width-4)
                row += 1
            
            if row < height - 4:
                if podcast.last_episode_date:
                    last_update = self.format_last_update(podcast.last_episode_date)
                    date_str = podcast.last_episode_date.strftime("%Y-%m-%d %H:%M")
                    self.safe_addstr(self.stdscr, row, 2, f"Last Episode: {last_update} ({date_str})", width-4)
                else:
                    self.safe_addstr(self.stdscr, row, 2, "Last Episode: Unknown", width-4)
                row += 2
            
            # Description (simplified)
            if podcast.description and row < height - 4:
                self.stdscr.attron(curses.A_BOLD)
                self.safe_addstr(self.stdscr, row, 2, "Description:", width-4)
                self.stdscr.attroff(curses.A_BOLD)
                row += 1
                
                # Simple description display
                desc_text = podcast.description[:width*3]  # Limit total length
                words = desc_text.split()
                current_line = ""
                line_width = width - 4
                
                for word in words:
                    if row >= height - 3:
                        break
                    if len(current_line + " " + word) <= line_width:
                        current_line += (" " if current_line else "") + word
                    else:
                        if current_line:
                            self.safe_addstr(self.stdscr, row, 2, current_line, width-4)
                            row += 1
                            if row >= height - 3:
                                break
                        current_line = word
                
                if current_line and row < height - 3:
                    self.safe_addstr(self.stdscr, row, 2, current_line, width-4)
            
            # Help
            self.safe_addstr(self.stdscr, height - 2, 2, "Press any key to return", width-4)
            
            self.stdscr.refresh()
            
            # Wait for keypress
            self.stdscr.getch()
            
        except Exception as e:
            log(f"Error showing details: {str(e)}")
            try:
                self.stdscr.clear()
                self.safe_addstr(self.stdscr, 0, 0, f"Display error: {str(e)}")
                self.safe_addstr(self.stdscr, 1, 0, "Press any key to return")
                self.stdscr.refresh()
                self.stdscr.getch()
            except:
                pass

    def run(self) -> None:
        """Main application loop"""
        try:
            self.init_curses()
        except Exception as e:
            print(f"Failed to initialize curses: {e}")
            return
        
        try:
            # Initial load
            self.load_subscriptions()
            selected_index = 0
            
            while self.running:
                try:
                    self.draw_main_screen(selected_index)
                except Exception as e:
                    log(f"Error in draw_main_screen: {str(e)}")
                    continue
                
                try:
                    key = self.stdscr.getch()
                except Exception as e:
                    log(f"Error getting key: {str(e)}")
                    break
                
                try:
                    if key == curses.KEY_UP and self.podcasts:
                        selected_index = max(0, selected_index - 1)
                    elif key == curses.KEY_DOWN and self.podcasts:
                        selected_index = min(len(self.podcasts) - 1, selected_index + 1)
                    elif key == ord('a'):  # Add subscription
                        try:
                            self.add_subscription()
                        except Exception as e:
                            log(f"Error adding subscription: {str(e)}")
                            self.set_status_message(f"Error adding subscription")
                    elif key == ord('d'):  # Delete subscription
                        if self.podcasts and 0 <= selected_index < len(self.podcasts):
                            try:
                                # Simple confirmation
                                self.set_status_message("Press 'y' to confirm deletion")
                                self.draw_main_screen(selected_index)
                                confirm_key = self.stdscr.getch()
                                
                                if confirm_key == ord('y'):
                                    self.remove_subscription(selected_index)
                                    selected_index = min(selected_index, len(self.podcasts) - 1) if self.podcasts else 0
                                else:
                                    self.set_status_message("Deletion cancelled")
                            except Exception as e:
                                log(f"Error in delete: {str(e)}")
                                self.set_status_message("Error deleting subscription")
                    elif key == ord('r'):  # Refresh
                        self.set_status_message("Refreshing subscriptions...")
                        self.load_subscriptions()
                    elif key in [curses.KEY_ENTER, 10, 13]:  # Show details
                        if self.podcasts and 0 <= selected_index < len(self.podcasts):
                            try:
                                self.show_podcast_details(selected_index)
                            except Exception as e:
                                log(f"Error showing details: {str(e)}")
                                self.set_status_message("Error showing details")
                    elif key in [27, ord('q')]:  # ESC or q - quit
                        self.running = False
                
                except Exception as e:
                    log(f"Error handling key {key}: {str(e)}")
                    continue
                    
        except KeyboardInterrupt:
            self.running = False
        except Exception as e:
            log(f"Fatal error in main loop: {str(e)}")
        finally:
            self.cleanup_curses()


if __name__ == "__main__":
    try:
        app = SubscriptionManager()
        app.run()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {str(e)}")
        log(f"Fatal error: {str(e)}")
