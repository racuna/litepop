#!/usr/bin/env python3
"""
litepop - Linux Terminal Podcast Player
Nextcloud-gPodder synchronization, playlist queue, and smart download
"""

import curses
import json
import os
import time
import threading
import subprocess
import requests
import configparser
import tempfile
import hashlib
import xml.etree.ElementTree as ET
import socket
import email.utils
from datetime import datetime, date  # Explicitly import date
from urllib.parse import urljoin
from typing import List, Dict, Optional
from pathlib import Path

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
            "server_url": "https://nch.pl",
            "username": "",
            "password": "",
            "sync_interval": "300"
        }
        self.config["player"] = {
            "temp_dir": "/tmp/litepop",
            "default_speed": "1.0",
            "player_command": "mpv --no-config --no-video --af=acompressor --speed={speed} --start={start_time} --input-ipc-server={ipc_socket} {file}"
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
    """Manages synchronization with gPodder server"""
    def __init__(self, config: Config):
        self.config = config
        self.server_url = self._normalize_url(config.get("gpodder", "server_url"))
        self.session = requests.Session()
        self.session.auth = (config.get("gpodder", "username"), config.get("gpodder", "password"))
        self.log_lock = threading.Lock()

    def _normalize_url(self, url: str) -> str:
        if not url.endswith("/"):
            url += "/"
        if "index.php/apps/gpoddersync" not in url:
            url = urljoin(url, "index.php/apps/gpoddersync/")
        return url

    def get_subscriptions(self, since: Optional[datetime] = None) -> List[str]:
        """Fetches subscription URLs from gPodder"""
        try:
            url = urljoin(self.server_url, "subscriptions")
            params = {"since": int(since.timestamp())} if since else {}
            response = self.session.get(url, headers={"User-Agent": "litepop/1.0"}, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            subscriptions = data.get("add", [])
            self._log(f"Retrieved {len(subscriptions)} subscriptions")
            return subscriptions
        except Exception as e:
            self._log(f"Error retrieving subscriptions: {str(e)}")
            return []

    def upload_subscription_changes(self, add_urls: List[str] = None, remove_urls: List[str] = None) -> Optional[Dict]:
        """Uploads subscription changes to gPodder"""
        try:
            url = urljoin(self.server_url, "subscription_change/create")
            data = {"add": add_urls or [], "remove": remove_urls or []}
            response = self.session.post(url, json=data, headers={"User-Agent": "litepop/1.0", "Content-Type": "application/json"}, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self._log(f"Error uploading subscription changes: {str(e)}")
            return None

    def get_episode_actions(self, since: Optional[datetime] = None) -> Dict:
        """Fetches episode actions from gPodder"""
        try:
            url = urljoin(self.server_url, "episode_action")
            params = {"since": int(since.timestamp())} if since else {}
            response = self.session.get(url, headers={"User-Agent": "litepop/1.0"}, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            self._log(f"Retrieved {len(data.get('actions', []))} episode actions")
            return data
        except Exception as e:
            self._log(f"Error retrieving episode actions: {str(e)}")
            return {"actions": []}

    def upload_episode_actions(self, actions: List[Dict]) -> Optional[Dict]:
        """Uploads episode actions to gPodder"""
        try:
            url = urljoin(self.server_url, "episode_action/create")
            response = self.session.post(url, json=actions, headers={"User-Agent": "litepop/1.0", "Content-Type": "application/json"}, timeout=30)
            response.raise_for_status()
            self._log(f"Uploaded {len(actions)} episode actions")
            return response.json()
        except Exception as e:
            self._log(f"Error uploading episode actions: {str(e)}")
            return None

    def _log(self, message: str) -> None:
        """Thread-safe logging to file"""
        with self.log_lock:
            with open("/tmp/litepop_debug.log", "a") as f:
                f.write(f"{datetime.now()}: {message}\n")

class PodcastFeed:
    """Represents a podcast feed with episodes"""
    def __init__(self, url: str):
        self.url = url
        self.title = "Untitled"
        self.episodes = []
        self.log_lock = threading.Lock()

    def fetch(self) -> bool:
        """Fetches and parses podcast feed"""
        try:
            response = requests.get(self.url, headers={"User-Agent": "litepop/1.0"}, timeout=30)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            channel = root.find("channel")
            if channel is not None:
                title_elem = channel.find("title")
                if title_elem is not None and title_elem.text:
                    self.title = title_elem.text.strip()

            self.episodes = [ep for ep in (self._parse_episode(item) for item in root.findall(".//item")) if ep]
            self.episodes.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
            self._log(f"Feed loaded: {self.title} - {len(self.episodes)} episodes")
            return True
        except Exception as e:
            self._log(f"Error loading feed {self.url}: {str(e)}")
            return False

    def _parse_episode(self, item: ET.Element) -> Optional[Dict]:
        """Parses a single episode from XML item"""
        title_elem = item.find("title")
        enclosure = item.find("enclosure")
        if title_elem is None or enclosure is None:
            return None

        pub_date = item.find("pubDate")
        description = item.find("description")
        guid_elem = item.find("guid")
        duration = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")

        return {
            "title": title_elem.text.strip() if title_elem.text else "Untitled",
            "url": enclosure.get("url"),
            "pub_date": pub_date.text if pub_date is not None else "",
            "description": description.text if description is not None else "",
            "podcast_title": self.title,
            "guid": guid_elem.text if guid_elem is not None else None,
            "duration": self._parse_duration(duration.text) if duration is not None and duration.text else None
        }

    def _parse_duration(self, duration_str: str) -> Optional[int]:
        """Parses duration string to seconds"""
        try:
            if ":" in duration_str:
                parts = duration_str.split(":")
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
            return int(duration_str)
        except:
            return None

    def _log(self, message: str) -> None:
        """Thread-safe logging to file"""
        with self.log_lock:
            with open("/tmp/litepop_debug.log", "a") as f:
                f.write(f"{datetime.now()}: {message}\n")

class Episode:
    """Represents a podcast episode"""
    def __init__(self, data: Dict):
        self.title = data["title"]
        self.url = data["url"]
        self.pub_date = data["pub_date"]
        self.description = data["description"]
        self.podcast_title = data["podcast_title"]
        self.guid = data.get("guid")
        self.duration = data.get("duration")
        self.position = 0
        self.completed = False
        self.local_file = None
        self.downloading = False
        self.progress = 0.0
        self.server_completed = False

    def __eq__(self, other) -> bool:
        if not isinstance(other, Episode):
            return NotImplemented
        return self.url == other.url

    def __hash__(self) -> int:
        return hash(self.url)

class DownloadManager:
    """Manages episode downloads"""
    def __init__(self, temp_dir: str, max_concurrent: int = 2):
        self.temp_dir = Path(temp_dir)
        self.max_concurrent = max_concurrent
        self.downloads = {}
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def get_episode_filename(self, episode: Episode) -> str:
        """Generates unique filename for episode"""
        url_hash = hashlib.md5(episode.url.encode()).hexdigest()
        return str(self.temp_dir / f"{url_hash}.mp3")

    def download_episode(self, episode: Episode, callback: Optional[callable] = None) -> None:
        """Downloads episode if not already downloaded"""
        with self.lock:
            if episode.url in self.downloads:
                return
            filename = self.get_episode_filename(episode)
            if Path(filename).exists():
                episode.local_file = filename
                return
            episode.downloading = True
            self.downloads[episode.url] = threading.Thread(target=self._download_worker, args=(episode, filename, callback))
            self.downloads[episode.url].daemon = True
            self.downloads[episode.url].start()

    def _download_worker(self, episode: Episode, filename: str, callback: Optional[callable]) -> None:
        """Worker function for downloading episodes"""
        try:
            response = requests.get(episode.url, stream=True, timeout=30)
            response.raise_for_status()
            with open(filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            episode.local_file = filename
            if callback:
                callback(episode)
        except Exception as e:
            with open("/tmp/litepop_debug.log", "a") as f:
                f.write(f"{datetime.now()}: Error downloading {episode.url}: {str(e)}\n")
        finally:
            with self.lock:
                episode.downloading = False
                if episode.url in self.downloads:
                    del self.downloads[episode.url]

    def cleanup_file(self, filename: str) -> None:
        """Removes specified file if it exists"""
        try:
            Path(filename).unlink(missing_ok=True)
        except Exception:
            pass

    def cleanup_all_files(self) -> None:
        """Removes all files in temp directory"""
        try:
            for file in self.temp_dir.glob("*.mp3"):
                file.unlink(missing_ok=True)
        except Exception:
            pass

class Player:
    """Manages audio playback using mpv"""
    def __init__(self, config: Config):
        self.config = config
        self.current_episode = None
        self.process = None
        self.speed = float(config.get("player", "default_speed", "1.0"))
        self.playing = False
        self.position = 0
        self.duration = 0
        self.ipc_socket = None
        self.position_monitor_thread = None
        self.position_lock = threading.Lock()
        self.log_lock = threading.Lock()

    def _create_ipc_socket(self) -> str:
        """Creates unique IPC socket path"""
        return str(Path(tempfile.gettempdir()) / f"mpv_socket_{os.getpid()}_{int(time.time())}")

    def _send_mpv_command(self, command: Dict) -> Optional[Dict]:
        """Sends command to mpv via IPC socket"""
        if not self.ipc_socket or not Path(self.ipc_socket).exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(self.ipc_socket)
                sock.sendall((json.dumps(command) + '\n').encode())
                response = b''
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    if b'\n' in chunk:
                        break
                response_str = response.decode().strip()
                return json.loads(response_str.split('\n')[-1])
        except Exception as e:
            if "Connection refused" not in str(e):
                self._log(f"IPC error: {str(e)}")
            return None

    def _monitor_position(self) -> None:
        """Monitors playback position via IPC"""
        while self.playing and self.process and self.process.poll() is None:
            try:
                pos_response = self._send_mpv_command({"command": ["get_property", "time-pos"]})
                if pos_response and pos_response.get("error") == "success":
                    with self.position_lock:
                        self.position = pos_response.get("data", 0.0)
                        if self.current_episode:
                            self.current_episode.position = self.position

                if not self.duration or (self.current_episode and self.current_episode.duration != self.duration):
                    dur_response = self._send_mpv_command({"command": ["get_property", "duration"]})
                    if dur_response and dur_response.get("error") == "success":
                        with self.position_lock:
                            self.duration = dur_response.get("data", 0.0)
                            if self.current_episode:
                                self.current_episode.duration = self.duration
                time.sleep(0.5)
            except Exception as e:
                self._log(f"Error monitoring position: {str(e)}")
                break

    def play(self, episode: Episode) -> bool:
        """Plays specified episode"""
        if not episode.local_file or not Path(episode.local_file).exists():
            self._log(f"Error: file not found for playback: {episode.local_file}")
            return False

        self.stop()
        self.current_episode = episode
        self.ipc_socket = self._create_ipc_socket()
        command = self.config.get("player", "player_command").format(
            speed=self.speed,
            file=episode.local_file,
            start_time=episode.position,
            ipc_socket=self.ipc_socket
        )

        try:
            self.process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.playing = True
            self.duration = episode.duration or 0
            self.position = episode.position or 0
            self.position_monitor_thread = threading.Thread(target=self._monitor_position)
            self.position_monitor_thread.daemon = True
            self.position_monitor_thread.start()

            def monitor_player():
                stdout, stderr = self.process.communicate()
                if self.process and self.process.returncode != 0:
                    self._log(f"Error in mpv (code {self.process.returncode}):\nSTDOUT: {stdout.decode('utf-8', errors='ignore')}\nSTDERR: {stderr.decode('utf-8', errors='ignore')}")
                self.playing = False
                if self.ipc_socket and Path(self.ipc_socket).exists():
                    Path(self.ipc_socket).unlink(missing_ok=True)

            threading.Thread(target=monitor_player, daemon=True).start()
            return True
        except Exception as e:
            self._log(f"Error starting mpv: {str(e)}")
            return False

    def stop(self) -> None:
        """Stops playback and cleans up"""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        self.playing = False
        if self.ipc_socket and Path(self.ipc_socket).exists():
            Path(self.ipc_socket).unlink(missing_ok=True)
            self.ipc_socket = None

    def set_speed(self, speed: float) -> None:
        """Sets playback speed"""
        self.speed = speed
        if self.playing and self.current_episode:
            if not self._send_mpv_command({"command": ["set_property", "speed", speed]}):
                self.play(self.current_episode)
        self._log(f"Speed changed to {speed}x")

    def seek(self, seconds: int) -> bool:
        """Seeks playback by specified seconds"""
        if not self.playing or not self.process or self.process.poll() is not None:
            return False
        if self._send_mpv_command({"command": ["seek", seconds]}):
            with self.position_lock:
                new_pos = max(0, min(self.position + seconds, self.duration or float('inf')))
                self.position = new_pos
                if self.current_episode:
                    self.current_episode.position = new_pos
            self._log(f"Seek via IPC: {seconds}s")
            return True
        if self.current_episode:
            with self.position_lock:
                new_pos = max(0, min(self.position + seconds, self.duration or float('inf')))
                self.position = new_pos
                self.current_episode.position = new_pos
            self._log(f"Seek via restart: {seconds}s")
            return self.play(self.current_episode)
        return False

    def get_position(self) -> float:
        """Returns current playback position in seconds"""
        return self.position if self.playing and self.current_episode else 0

    def get_duration(self) -> float:
        """Returns total duration in seconds"""
        return self.duration if self.current_episode and self.duration else 0

    def format_time(self, seconds: float) -> str:
        """Formats seconds to HH:MM:SS"""
        if not seconds or seconds < 0:
            return "00:00:00"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _log(self, message: str) -> None:
        """Thread-safe logging to file"""
        with self.log_lock:
            with open("/tmp/litepop_debug.log", "a") as f:
                f.write(f"{datetime.now()}: {message}\n")

class Litepop:
    """Main application class for litepop"""
    def __init__(self):
        self.config = Config()
        Path("/tmp/litepop_debug.log").write_text("")
        self.gpodder = GPodderSync(self.config)
        self.download_manager = DownloadManager(self.config.get("player", "temp_dir", "/tmp/litepop"))
        self.player = Player(self.config)
        self.queue = []
        self.current_index = -1
        self.subscriptions = []
        self.last_sync = None
        self.running = True
        self.status_message = None
        self.status_timeout = None
        self.stdscr = None
        self.current_screen = "main"
        self.last_log_line = ""
        self.episode_actions_cache = {}
        self.ui_refresh_lock = threading.Lock()
        self.initial_sync_done = False
        self.threads = [
            threading.Thread(target=self._sync_worker, daemon=True),
            threading.Thread(target=self._playback_monitor, daemon=True),
            threading.Thread(target=self._log_monitor, daemon=True),
            threading.Thread(target=self._position_sync_worker, daemon=True)
        ]

    def init_curses(self) -> None:
        """Initializes curses for terminal UI"""
        self.stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.stdscr.keypad(True)
        curses.curs_set(0)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(7, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLACK)

    def cleanup_curses(self) -> None:
        """Cleans up curses settings"""
        if self.stdscr:
            curses.nocbreak()
            self.stdscr.keypad(False)
            curses.echo()
            curses.endwin()

    def _log_monitor(self) -> None:
        """Monitors log file for last line"""
        log_file = Path("/tmp/litepop_debug.log")
        while self.running:
            try:
                if log_file.exists():
                    lines = log_file.read_text().splitlines()
                    self.last_log_line = lines[-1].strip() if lines else ""
                time.sleep(0.5)
            except Exception:
                time.sleep(1)

    def _position_sync_worker(self) -> None:
        """Syncs playback position to gPodder"""
        while self.running:
            try:
                if self.player.playing and 0 <= self.current_index < len(self.queue):
                    episode = self.queue[self.current_index]
                    action = {
                        "podcast": episode.podcast_title,
                        "episode": episode.url,
                        "action": "PLAY",
                        "timestamp": datetime.now().isoformat(),
                        "position": int(self.player.get_position()),
                        "started": 0,
                        "total": self.player.get_duration() or -1,
                        "guid": episode.guid
                    }
                    self.gpodder.upload_episode_actions([action])
                time.sleep(30)
            except Exception as e:
                with open("/tmp/litepop_debug.log", "a") as f:
                    f.write(f"{datetime.now()}: Error in position sync: {str(e)}\n")
                time.sleep(60)

    def _sync_worker(self) -> None:
        """Handles periodic sync with gPodder"""
        sync_interval = int(self.config.get("gpodder", "sync_interval", "300"))
        self._sync_with_gpodder()
        self.initial_sync_done = True
        while self.running:
            time.sleep(sync_interval)
            if self.running:
                self._sync_with_gpodder()

    def _playback_monitor(self) -> None:
        """Monitors playback status and auto-plays next episode"""
        while self.running:
            if self.player.process and self.player.process.poll() is not None:
                self.player.playing = False
                if self.current_index >= 0 and self.current_index < len(self.queue):
                    episode = self.queue[self.current_index]
                    if self.player.get_duration() and self.player.get_position() >= self.player.get_duration() * 0.95:
                        self.mark_episode_completed(episode)
                        self.play_next()
                    else:
                        self._sync_episode_position(episode)
            time.sleep(0.5)

    def _sync_with_gpodder(self) -> bool:
        """Syncs subscriptions and episode actions with gPodder"""
        try:
            if not self.config.get("gpodder", "username") or not self.config.get("gpodder", "password"):
                return False
            local_actions = self._get_pending_actions()
            if local_actions:
                self.gpodder.upload_episode_actions(local_actions)
            actions_data = self.gpodder.get_episode_actions()
            self._update_episode_actions_cache(actions_data.get("actions", []))
            subscriptions = self.gpodder.get_subscriptions(self.last_sync)
            if not subscriptions and self.subscriptions:
                for feed in self.subscriptions:
                    feed.fetch()
                self.last_sync = datetime.now()
                self._load_auto_queue()
                return True
            if not subscriptions:
                return False

            new_feeds = []
            threads = []
            feed_lock = threading.Lock()

            def fetch_feed_threaded(sub_url: str) -> None:
                feed = PodcastFeed(sub_url)
                if feed.fetch():
                    with feed_lock:
                        new_feeds.append(feed)

            for sub_url in subscriptions:
                thread = threading.Thread(target=fetch_feed_threaded, args=(sub_url,))
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join()

            self.subscriptions = new_feeds
            self.last_sync = datetime.now()
            self._load_auto_queue()
            return True
        except Exception as e:
            with open("/tmp/litepop_debug.log", "a") as f:
                f.write(f"{datetime.now()}: Error in sync: {str(e)}\n")
            return False

    def _update_episode_actions_cache(self, actions: List[Dict]) -> None:
        """Updates episode actions cache"""
        for action in actions:
            episode_url = action.get("episode")
            if episode_url:
                self.episode_actions_cache.setdefault(episode_url, {
                    "progress": 0.0,
                    "position": 0,
                    "total": -1,
                    "server_completed": False
                })
                if action.get("action") == "PLAY":
                    position = action.get("position", 0)
                    total = action.get("total", -1)
                    progress = (position / total) * 100 if total > 0 else 0.0
                    self.episode_actions_cache[episode_url].update({
                        "position": position,
                        "total": total,
                        "progress": progress,
                        "server_completed": progress > 95.0
                    })
                elif action.get("action") == "DOWNLOAD":
                    self.episode_actions_cache[episode_url].update({
                        "server_completed": True,
                        "progress": 100.0
                    })
                with open("/tmp/litepop_debug.log", "a") as f:
                    f.write(f"{datetime.now()}: Updated cache for {episode_url}: {self.episode_actions_cache[episode_url]}\n")

    def _get_episode_server_status(self, episode_url: str) -> Dict:
        """Gets episode status from cache"""
        default_status = {"progress": 0.0, "position": 0, "server_completed": False, "total": -1}
        status = self.episode_actions_cache.get(episode_url, default_status)
        for key in default_status:
            if key not in status:
                status[key] = default_status[key]
        with open("/tmp/litepop_debug.log", "a") as f:
            f.write(f"{datetime.now()}: Fetched status for {episode_url}: {status}\n")
        return status

    def _load_auto_queue(self) -> None:
        """Loads partially played episodes into queue"""
        for episode in self.queue:
            server_status = self._get_episode_server_status(episode.url)
            episode.server_completed = server_status["server_completed"]
            episode.position = max(episode.position, server_status["position"])
            episode.progress = max(episode.progress, server_status["progress"])

        for episode_url, cache_data in self.episode_actions_cache.items():
            progress = cache_data.get("progress", 0.0)
            if 1 <= progress < 95 and not any(ep.url == episode_url for ep in self.queue):
                for feed in self.subscriptions:
                    for episode_data in feed.episodes:
                        if episode_data["url"] == episode_url:
                            episode = Episode(episode_data)
                            episode.progress = progress
                            episode.position = cache_data.get("position", 0)
                            episode.server_completed = cache_data.get("server_completed", False)
                            self.queue.append(episode)
                            self.download_manager.download_episode(episode)
                            break

    def _get_pending_actions(self) -> List[Dict]:
        """Gets pending episode actions for upload"""
        actions = []
        if self.player.playing and 0 <= self.current_index < len(self.queue):
            episode = self.queue[self.current_index]
            current_position = self.player.get_position()
            if current_position > episode.position:
                episode.position = current_position
            actions.append({
                "podcast": episode.podcast_title,
                "episode": episode.url,
                "action": "PLAY",
                "timestamp": datetime.now().isoformat(),
                "position": int(episode.position),
                "started": 0,
                "total": episode.duration or -1,
                "guid": episode.guid
            })

        for episode in self.queue:
            if episode.completed:
                actions.append({
                    "podcast": episode.podcast_title,
                    "episode": episode.url,
                    "action": "DOWNLOAD",
                    "timestamp": datetime.now().isoformat(),
                    "position": None,
                    "started": -1,
                    "total": None,
                    "guid": episode.guid
                })
            elif episode.position > 0 and episode != (self.queue[self.current_index] if 0 <= self.current_index < len(self.queue) else None):
                actions.append({
                    "podcast": episode.podcast_title,
                    "episode": episode.url,
                    "action": "PLAY",
                    "timestamp": datetime.now().isoformat(),
                    "position": int(episode.position),
                    "started": 0,
                    "total": episode.duration or -1,
                    "guid": episode.guid
                })
        return actions

    def mark_episode_completed(self, episode: Episode) -> None:
        """Marks episode as completed and uploads status"""
        episode.completed = True
        episode.progress = 100.0
        episode.server_completed = True
        action = {
            "podcast": episode.podcast_title,
            "episode": episode.url,
            "action": "DOWNLOAD",
            "timestamp": datetime.now().isoformat(),
            "position": None,
            "started": -1,
            "total": None,
            "guid": episode.guid
        }
        if self.gpodder.upload_episode_actions([action]):
            with open("/tmp/litepop_debug.log", "a") as f:
                f.write(f"{datetime.now()}: Episode marked as completed: {episode.title}\n")
        if episode.local_file:
            self.download_manager.cleanup_file(episode.local_file)

    def draw_header(self) -> None:
        """Draws header for UI"""
        height, width = self.stdscr.getmaxyx()
        header = " litepop - Playback Queue "
        self.stdscr.attron(curses.color_pair(1))
        self.stdscr.addstr(0, 0, header.center(width))
        self.stdscr.attroff(curses.color_pair(1))

    def draw_queue(self, selected_index: int = 0) -> None:
        """Draws playback queue UI"""
        with self.ui_refresh_lock:
            self.stdscr.clear()
            self.draw_header()
            height, width = self.stdscr.getmaxyx()
            status = "Stopped"
            if self.player.playing and 0 <= self.current_index < len(self.queue):
                episode = self.queue[self.current_index]
                pos_str = self.player.format_time(self.player.get_position())
                dur_str = self.player.format_time(self.player.get_duration())
                title = episode.title[:max(25, width - 50)]
                if len(episode.title) > max(25, width - 50):
                    title += "..."
                status = f"Playing: {title} (x{self.player.speed}) ({pos_str}/{dur_str})"

            self.stdscr.addstr(2, 2, f"Status: {status}")
            sync_status = f"Subscriptions: {len(self.subscriptions)} | Last sync: {self.last_sync.strftime('%H:%M') if self.last_sync else 'Never'}"
            self.stdscr.addstr(3, 2, sync_status)
            start_row = 5
            visible_items = height - 11

            if not self.queue:
                self.stdscr.addstr(start_row, 2, "Queue empty. Press 'a' to add episodes.")
                if not self.subscriptions:
                    self.stdscr.addstr(start_row + 1, 2, "No subscriptions. Check gPodder config.")
            else:
                for i, episode in enumerate(self.queue[selected_index:selected_index + visible_items]):
                    row = start_row + i
                    actual_index = selected_index + i
                    title = episode.title[:width-30] + "..." if len(episode.title) > width-30 else episode.title
                    status_icon = " "
                    color_pair = 0
                    if episode.downloading:
                        status_icon, color_pair = "↓", 4
                    elif episode.completed or episode.server_completed:
                        status_icon, color_pair = "✔", 6
                    elif actual_index == self.current_index:
                        status_icon = "♪" if self.player.playing else "II"
                        color_pair = 3

                    if color_pair:
                        self.stdscr.attron(curses.color_pair(color_pair))
                    if actual_index == selected_index:
                        self.stdscr.attron(curses.color_pair(2))
                    line = f"{status_icon} {title} - {episode.podcast_title} [{episode.progress:.1f}%]" + (" ←" if actual_index == selected_index and actual_index != self.current_index else "")
                    self.stdscr.addstr(row, 2, line[:width-4])
                    if actual_index == selected_index:
                        self.stdscr.attroff(curses.color_pair(2))
                    if color_pair:
                        self.stdscr.attroff(curses.color_pair(color_pair))

            help_lines = [
                f"SPACE:Play/Pause/Switch | ENTER:Next | ←→:±10s | d:Delete | D:Delete/Mark done | a:Add | s:Speed({self.player.speed}x) | R:Reset Progress",
                "c:Clear completed | r:Sync now | ESC/q:Exit | ♪:Playing II:Paused ←:Selected"
            ]
            for i, line in enumerate(help_lines):
                self.stdscr.addstr(height - 4 + i, 2, line[:width-4])
            if self.last_log_line:
                self.stdscr.attron(curses.color_pair(7))
                self.stdscr.addstr(height - 2, 2, f"Log: {self.last_log_line}"[:width-4])
                self.stdscr.attroff(curses.color_pair(7))
            if self.status_message and time.time() < self.status_timeout:
                self.stdscr.attron(curses.color_pair(5))
                self.stdscr.addstr(height - 5, 2, self.status_message[:width-4])
                self.stdscr.attroff(curses.color_pair(5))
            self.stdscr.refresh()

    def add_episodes_screen(self) -> None:
        """Displays screen for adding episodes"""
        if not self.initial_sync_done:
            self.set_status_message("Please wait for initial sync to complete.")
            return

        height, width = self.stdscr.getmaxyx()
        selected = 0
        all_episodes = []
        for feed in self.subscriptions:
            for episode in feed.episodes:
                if not any(ep.url == episode["url"] for ep in self.queue):
                    episode_obj = Episode(episode)
                    server_status = self._get_episode_server_status(episode_obj.url)
                    episode_obj.server_completed = server_status.get("server_completed", False)
                    episode_obj.progress = server_status.get("progress", 0.0)
                    all_episodes.append(episode_obj)

        all_episodes.sort(key=lambda ep: email.utils.parsedate_to_datetime(ep.pub_date).timestamp() if ep.pub_date else 0, reverse=True)
        display_items = []
        current_date = None
        for episode in all_episodes:
            try:
                parsed_date = email.utils.parsedate_to_datetime(episode.pub_date) if episode.pub_date else None
                date_obj = parsed_date.date() if parsed_date else None
                with open("/tmp/litepop_debug.log", "a") as f:
                    f.write(f"{datetime.now()}: Processing episode {episode.title}, pub_date: {episode.pub_date}, parsed_date: {parsed_date}, date_obj: {date_obj}, type: {type(date_obj)}\n")
            except Exception as e:
                with open("/tmp/litepop_debug.log", "a") as f:
                    f.write(f"{datetime.now()}: Error parsing pub_date for {episode.title}: {str(e)}\n")
                date_obj = None

            date_str = date_obj.strftime('%Y-%m-%d') if isinstance(date_obj, date) else 'No date'
            if current_date != date_obj:
                current_date = date_obj
                display_items.append({'type': 'separator', 'text': f"------- {date_str} -------"})
            display_items.append({'type': 'episode', 'episode': episode})

        if not display_items:
            self.set_status_message("No new episodes to add.")
            return

        scroll_offset = 0
        while True:
            self.stdscr.clear()
            self.draw_header()
            self.stdscr.addstr(2, 2, "Add Episodes (Press ENTER to add, ESC to return)")
            visible_items = height - 7
            for i, item in enumerate(display_items[scroll_offset:scroll_offset + visible_items]):
                row = 4 + i
                if item['type'] == 'separator':
                    self.stdscr.attron(curses.A_BOLD)
                    self.stdscr.addstr(row, 2, item['text'][:width-4])
                    self.stdscr.attroff(curses.A_BOLD)
                else:
                    episode = item['episode']
                    title = episode.title[:width-10] + "..." if len(episode.title) > width-10 else episode.title
                    line = f"  {title} - {episode.podcast_title} [{episode.progress:.1f}%]"
                    if episode.server_completed:
                        self.stdscr.attron(curses.color_pair(8))
                    if i + scroll_offset == selected:
                        self.stdscr.attron(curses.color_pair(2))
                    self.stdscr.addstr(row, 2, line[:width-4])
                    if i + scroll_offset == selected:
                        self.stdscr.attroff(curses.color_pair(2))
                    if episode.server_completed:
                        self.stdscr.attroff(curses.color_pair(8))

            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key == curses.KEY_UP:
                selected = max(0, selected - 1)
                if selected < scroll_offset:
                    scroll_offset = selected
            elif key == curses.KEY_DOWN:
                selected = min(len(display_items) - 1, selected + 1)
                if selected >= scroll_offset + visible_items:
                    scroll_offset = selected - visible_items + 1
            elif key in [curses.KEY_ENTER, 10, 13] and 0 <= selected < len(display_items) and display_items[selected]['type'] == 'episode':
                episode = display_items[selected]['episode']
                self.queue.append(episode)
                self.download_manager.download_episode(episode)
                self.set_status_message(f"Added: {episode.title}")
                display_items.pop(selected)
                if selected >= len(display_items) and display_items:
                    selected = len(display_items) - 1
                elif not display_items:
                    return
            elif key == 27:
                break

    def set_status_message(self, message: str, timeout: int = 3) -> None:
        """Sets temporary status message"""
        self.status_message = message
        self.status_timeout = time.time() + timeout

    def play_selected(self, index: int) -> bool:
        """Plays selected episode"""
        if 0 <= index < len(self.queue):
            self.player.play(self.queue[index])
            self.current_index = index
            self.set_status_message(f"Playing: {self.queue[index].title}")
            return True
        return False

    def play_next(self) -> bool:
        """Plays next episode in queue"""
        if self.current_index + 1 < len(self.queue):
            return self.play_selected(self.current_index + 1)
        self.player.stop()
        self.set_status_message("End of queue.")
        return False

    def play_previous(self) -> bool:
        """Plays previous episode in queue"""
        if self.current_index - 1 >= 0:
            return self.play_selected(self.current_index - 1)
        self.player.stop()
        self.set_status_message("Beginning of queue.")
        return False

    def delete_episode(self, index: int) -> bool:
        """Deletes episode from queue"""
        if 0 <= index < len(self.queue):
            episode = self.queue.pop(index)
            if episode.local_file:
                self.download_manager.cleanup_file(episode.local_file)
            if index == self.current_index:
                self.player.stop()
                self.current_index = -1
            elif index < self.current_index:
                self.current_index -= 1
            self.set_status_message(f"Deleted: {episode.title}")
            return True
        return False

    def delete_and_mark_done(self, index: int) -> bool:
        """Deletes episode and marks as completed"""
        if 0 <= index < len(self.queue):
            self.mark_episode_completed(self.queue[index])
            return self.delete_episode(index)
        return False

    def clear_completed_episodes(self) -> None:
        """Clears completed episodes from queue"""
        initial_len = len(self.queue)
        self.queue = [ep for ep in self.queue if not (ep.completed or ep.server_completed)]
        if self.current_index >= len(self.queue):
            self.current_index = -1
            self.player.stop()
        self.set_status_message(f"Cleaned up {initial_len - len(self.queue)} completed episodes.")

    def _sync_episode_position(self, episode: Episode) -> None:
        """Syncs episode position to gPodder"""
        action = {
            "podcast": episode.podcast_title,
            "episode": episode.url,
            "action": "PLAY",
            "timestamp": datetime.now().isoformat(),
            "position": int(episode.position),
            "started": 0,
            "total": episode.duration or -1,
            "guid": episode.guid
        }
        self.gpodder.upload_episode_actions([action])

    def run(self) -> None:
        """Main application loop"""
        self.init_curses()
        for thread in self.threads:
            thread.start()
        while not self.initial_sync_done:
            self.draw_queue(selected_index=0)
            self.stdscr.addstr(5, 2, "Performing initial sync, please wait...")
            self.stdscr.refresh()
            time.sleep(0.5)
        selected_index = 0
        try:
            while self.running:
                self.draw_queue(selected_index)
                key = self.stdscr.getch()
                if key == curses.KEY_UP:
                    selected_index = max(0, selected_index - 1)
                elif key == curses.KEY_DOWN:
                    selected_index = min(len(self.queue) - 1, selected_index + 1)
                elif key == ord(' '):
                    if self.current_index == selected_index:
                        if self.player.playing:
                            self.player.stop()
                            self.set_status_message("Playback paused.")
                        else:
                            self.player.play(self.queue[selected_index])
                            self.set_status_message("Playback resumed.")
                    else:
                        self.play_selected(selected_index)
                elif key in [curses.KEY_ENTER, 10, 13]:
                    self.play_next()
                elif key == curses.KEY_LEFT:
                    self.player.seek(-10)
                    self.set_status_message("Seeked -10s.")
                elif key == curses.KEY_RIGHT:
                    self.player.seek(10)
                    self.set_status_message("Seeked +10s.")
                elif key == ord('d'):
                    if self.delete_episode(selected_index):
                        selected_index = min(selected_index, len(self.queue) - 1) if self.queue else 0
                elif key == ord('D'):
                    if self.delete_and_mark_done(selected_index):
                        selected_index = min(selected_index, len(self.queue) - 1) if self.queue else 0
                elif key == ord('a'):
                    self.add_episodes_screen()
                    selected_index = min(selected_index, len(self.queue) - 1) if self.queue else 0
                elif key == ord('s'):
                    self.player.set_speed({1.0: 1.5, 1.5: 2.0, 2.0: 0.5}.get(self.player.speed, 1.0))
                    self.set_status_message(f"Speed set to {self.player.speed}x")
                elif key == ord('R'):
                    if 0 <= selected_index < len(self.queue):
                        episode = self.queue[selected_index]
                        episode.position = 0
                        episode.completed = False
                        episode.server_completed = False
                        self.set_status_message(f"Progress reset for: {episode.title}")
                        self._sync_episode_position(episode)
                elif key == ord('c'):
                    self.clear_completed_episodes()
                elif key == ord('r'):
                    self.set_status_message("Syncing with gPodder...")
                    self._sync_with_gpodder()
                    self.set_status_message("Sync complete.")
                elif key in [27, ord('q')]:
                    self.running = False
        finally:
            self.player.stop()
            self.download_manager.cleanup_all_files()
            self.cleanup_curses()

if __name__ == "__main__":
    app = Litepop()
    app.run()
