#!/usr/bin/env python3
"""
litepop - Linux Terminal Podcast Player
Nextcloud-gPodder and opodsync synchronization, playlist queue, and smart download
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

def log(msg: str) -> None:
    """Global logging function"""
    with open("/tmp/litepop_debug.log", "a") as f:
        f.write(f"{datetime.now()}: {msg}\n")

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
            "initial_days_back ": "90",
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
        
        self.config = config

        self.session = requests.Session()
        self.session.auth = (self.username, self.password)
        self.episode_actions_cache: List[Dict] = []
        self.subscriptions_cache: List[str] = []
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
                
            resp = self.session.get(url, headers={"User-Agent": "litepop/1.0"}, timeout=15)
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
            resp = self.session.get(url, headers={"User-Agent": "litepop/1.0"}, timeout=30)
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

            self.subscriptions_cache = subscriptions
            log(f"Retrieved {len(subscriptions)} subscriptions")
            return subscriptions
            
        except Exception as e:
            log(f"Error retrieving subscriptions: {str(e)}")
            return []

    def get_episode_actions(self, since: Optional[datetime] = None) -> Dict:
        """Get episode actions from server with improved parsing"""
        try:
            if self.backend == "nextcloud":
                url = urljoin(self.server_url, "episode_action")
            elif self.backend == "opodsync":
                url = urljoin(self.server_url, f"api/2/episodes/{self.username}.json")
            else:
                raise ValueError(f"Unknown backend: {self.backend}")

            params = {}
            if self.backend == "opodsync":
                if since:
                    # opodsync expects Unix timestamp
                    params["since"] = int(since.timestamp())
                else:
                    # Primera sincronización: limitar a últimos N días (por defecto 90)
                    try:
                        days_back = int(self.config.get("gpodder", "initial_days_back", "90"))
                    except ValueError:
                        days_back = 90
                    cutoff = int(datetime.now().timestamp()) - (days_back * 86400)
                    params["since"] = cutoff
                    log(f"Using initial sync cutoff: last {days_back} days (since={params['since']})")

            log(f"Fetching episode actions from: {url} with params: {params}")
            resp = self.session.get(url, headers={"User-Agent": "litepop/1.0"}, params=params, timeout=30)
            
            # AÑADIR: Mejor logging de la respuesta
            log(f"Episode actions response status: {resp.status_code}")
            log(f"Episode actions response headers: {dict(resp.headers)}")
            log(f"Episode actions response content length: {len(resp.content) if resp.content else 0}")
            
            resp.raise_for_status()
            
            # MEJORAR: Mejor manejo de respuestas vacías
            if not resp.content or len(resp.content.strip()) == 0:
                log("Empty response from episode actions endpoint")
                return {"actions": [], "timestamp": int(datetime.now().timestamp())}
            
            # AÑADIR: Log del contenido de la respuesta (primeros 200 caracteres)
            content_preview = resp.text[:200] + "..." if len(resp.text) > 200 else resp.text
            log(f"Episode actions raw response preview: {content_preview}")
            
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                log(f"JSON decode error: {str(e)}")
                log(f"Raw response text: {resp.text}")
                return {"actions": [], "timestamp": int(datetime.now().timestamp())}
            
            log(f"Episode actions parsed JSON type: {type(data)}")
            
            # Handle different response formats more robustly
            actions = []
            timestamp = int(datetime.now().timestamp())
            
            if isinstance(data, dict):
                # Standard gPodder format
                if "actions" in data:
                    actions = data.get("actions", [])
                    timestamp = data.get("timestamp", timestamp)
                    log(f"Found 'actions' key with {len(actions)} actions")
                # Some servers return actions directly in dict
                elif "podcast" in data or "episode" in data:
                    actions = [data]
                    log("Found single action in dict format")
                # Nextcloud format might have different structure
                else:
                    # Look for any list in the response
                    for key, value in data.items():
                        if isinstance(value, list):
                            actions = value
                            log(f"Found actions in key '{key}' with {len(actions)} items")
                            break
                            
            elif isinstance(data, list):
                # Direct list of actions
                actions = data
                log(f"Found direct list with {len(actions)} actions")
            else:
                log(f"Unexpected response format: {type(data)}")
                actions = []

            # Validate and clean actions
            valid_actions = []
            for i, action in enumerate(actions):
                if isinstance(action, dict) and "episode" in action and "action" in action:
                    # Ensure required fields exist
                    cleaned_action = {
                        "podcast": action.get("podcast", ""),
                        "episode": action.get("episode", ""),
                        "action": action.get("action", "").lower(),
                        "timestamp": action.get("timestamp", ""),
                        "device": action.get("device", ""),
                    }
                    
                    # Add optional numeric fields if valid
                    for field in ["position", "started", "total"]:
                        if field in action:
                            try:
                                value = int(float(action[field])) if action[field] is not None else 0
                                if value >= 0:
                                    cleaned_action[field] = value
                            except (ValueError, TypeError):
                                pass
                    
                    # Add guid if present and not empty
                    if action.get("guid") and str(action["guid"]).strip():
                        cleaned_action["guid"] = str(action["guid"]).strip()
                    
                    valid_actions.append(cleaned_action)
                else:
                    log(f"Skipping invalid action {i}: {action}")

            log(f"Retrieved {len(valid_actions)} valid episode actions")
            self.episode_actions_cache = valid_actions
            return {"actions": valid_actions, "timestamp": timestamp}
            
        except Exception as e:
            log(f"Error retrieving episode actions: {str(e)}")
            import traceback
            log(f"Full traceback: {traceback.format_exc()}")
            return {"actions": [], "timestamp": int(datetime.now().timestamp())}

    def upload_episode_actions(self, actions: List[Dict]) -> Dict:
        """Upload episode actions with improved timestamp handling"""
        try:
            if not actions:
                return {}

            # Format actions according to backend requirements
            formatted_actions = []
            for action in actions:
                # Create base action with required fields
                formatted_action = {
                    "podcast": str(action.get("podcast", "")),
                    "episode": str(action.get("episode", "")),
                    "action": str(action.get("action", "")).lower(),
                }
            
                # Handle timestamp with multiple format attempts
                timestamp = action.get("timestamp")
                if timestamp:
                    try:
                        # Parse various timestamp formats
                        if isinstance(timestamp, str):
                            # Try ISO format first
                            if 'T' in timestamp:
                                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            # Try Unix timestamp string
                            elif timestamp.isdigit():
                                dt = datetime.fromtimestamp(int(timestamp))
                            else:
                                # Fallback to current time
                                dt = datetime.now()
                        elif isinstance(timestamp, (int, float)):
                            dt = datetime.fromtimestamp(timestamp)
                        else:
                            dt = datetime.now()
                    
                        # Format timestamp based on backend
                        if self.backend == "opodsync":
                            # opodsync prefers ISO format without microseconds and with Z suffix
                            formatted_action["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        else:
                            # Nextcloud format
                            formatted_action["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
                            
                    except Exception as e:
                        log(f"Error formatting timestamp {timestamp}: {str(e)}")
                        dt = datetime.now()
                        formatted_action["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ" if self.backend == "opodsync" else "%Y-%m-%dT%H:%M:%S")
                else:
                    dt = datetime.now()
                    formatted_action["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ" if self.backend == "opodsync" else "%Y-%m-%dT%H:%M:%S")
            
                # Add device field for opodsync (required)
                if self.backend == "opodsync":
                    formatted_action["device"] = self.device_id
            
                # Add optional fields if present and valid
                for field in ["position", "started", "total"]:
                    if field in action and action[field] is not None:
                        try:
                            value = int(float(action[field]))
                            if field == "total":
                                if value > 0:
                                    formatted_action[field] = value
                                else:
                                    if value >= 0:
                                        formatted_action[field] = value
                            elif value >= 0:
                                formatted_action[field] = value
                        except (ValueError, TypeError):
                            pass
            
                # Only add guid if it's not empty
                if "guid" in action and action["guid"] and str(action["guid"]).strip():
                    formatted_action["guid"] = str(action["guid"]).strip()
            
                formatted_actions.append(formatted_action)

            # Choose the correct endpoint
            if self.backend == "nextcloud":
                url = urljoin(self.server_url, "episode_action/create")
            elif self.backend == "opodsync":
                url = urljoin(self.server_url, f"api/2/episodes/{self.username}.json")
            else:
                raise ValueError(f"Unknown backend: {self.backend}")

            log(f"Uploading {len(formatted_actions)} episode actions to: {url}")
            log(f"Sample action: {json.dumps(formatted_actions[0] if formatted_actions else {}, indent=2)}")
        
            # Prepare headers
            headers = {
                "User-Agent": "litepop/1.0",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
        
            # Make the request
            resp = self.session.post(
                url,
                headers=headers,
                json=formatted_actions,
                timeout=30,
            )
        
            log(f"Episode actions upload response status: {resp.status_code}")
        
            # Log response content for debugging
            if resp.content:
                try:
                    response_text = resp.text[:500] + "..." if len(resp.text) > 500 else resp.text
                    log(f"Episode actions upload response: {response_text}")
                except:
                    log(f"Episode actions upload response (bytes): {resp.content[:200]}...")
        
            resp.raise_for_status()
        
            # Parse response if present
            if resp.content:
                try:
                    result = resp.json()
                    return result
                except ValueError:
                    return {"status": "success"}
            else:
                return {"status": "success"}
            
        except requests.exceptions.HTTPError as e:
            log(f"HTTP error uploading episode actions: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                log(f"Response status: {e.response.status_code}")
                try:
                    log(f"Response content: {e.response.text}")
                except:
                    log(f"Response content (bytes): {e.response.content}")
            return {"error": str(e)}
        except Exception as e:
            log(f"Error uploading episode actions: {str(e)}")
            return {"error": str(e)}

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
            log(f"Fetching feed: {self.url}")
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
            log(f"Feed loaded: {self.title} - {len(self.episodes)} episodes")
            return True
        except Exception as e:
            log(f"Error loading feed {self.url}: {str(e)}")
            return False

    def _parse_episode(self, item: ET.Element) -> Optional[Dict]:
        """Parses a single episode from XML item"""
        title_elem = item.find("title")
        # enclosure may be in namespace or direct
        enclosure = item.find("enclosure")
        if enclosure is None:
            # try any namespace / child with tag ending 'enclosure'
            for child in item:
                if child.tag.lower().endswith('enclosure'):
                    enclosure = child
                    break

        if title_elem is None or enclosure is None:
            return None

        pub_date = item.find("pubDate")
        description = item.find("description")
        guid_elem = item.find("guid")
        # itunes duration namespace (some feeds use different namespace variants)
        duration = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
        if duration is None:
            # fallback: search for tag name ending with 'duration'
            for child in item:
                if child.tag.lower().endswith('duration'):
                    duration = child
                    break

        return {
            "title": title_elem.text.strip() if title_elem.text else "Untitled",
            "url": enclosure.get("url") if enclosure is not None else None,
            "pub_date": pub_date.text if pub_date is not None else "",
            "description": description.text if description is not None else "",
            "podcast_title": self.title,
            "podcast": self.url,           # <-- important: include feed URL
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

class Episode:
    """Represents a podcast episode"""
    def __init__(self, data: Dict):
        self.title = data["title"]
        self.url = data["url"]
        self.pub_date = data.get("pub_date")
        self.description = data.get("description")
        self.podcast_title = data.get("podcast_title")
        self.podcast_url = data.get("podcast")  # <-- nuevo campo
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
            log(f"Starting download: {episode.title}")
            response = requests.get(episode.url, stream=True, timeout=30, headers={"User-Agent": "litepop/1.0"})
            response.raise_for_status()
            with open(filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            episode.local_file = filename
            log(f"Download completed: {episode.title}")
            if callback:
                callback(episode)
        except Exception as e:
            log(f"Error downloading {episode.url}: {str(e)}")
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
                log(f"IPC error: {str(e)}")
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
                log(f"Error monitoring position: {str(e)}")
                break

    def play(self, episode: Episode) -> bool:
        """Plays specified episode"""
        if not episode.local_file or not Path(episode.local_file).exists():
            log(f"Error: file not found for playback: {episode.local_file}")
            return False

        self.stop()
        self.current_episode = episode
        self.ipc_socket = self._create_ipc_socket()
        command = self.config.get("player", "player_command").format(
            speed=self.speed,
            file=episode.local_file,
            start_time=episode.position or 0,
            ipc_socket=self.ipc_socket
        )

        try:
            log(f"Starting playback: {episode.title} at position {episode.position}")
            self.process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.playing = True
            self.duration = episode.duration or 0
            self.position = episode.position or 0
            time.sleep(0.5)
            self.position_monitor_thread = threading.Thread(target=self._monitor_position)
            self.position_monitor_thread.daemon = True
            self.position_monitor_thread.start()

            def monitor_player():
                stdout, stderr = self.process.communicate()
                if self.process and self.process.returncode != 0:
                    log(f"Error in mpv (code {self.process.returncode}):\nSTDOUT: {stdout.decode('utf-8', errors='ignore')}\nSTDERR: {stderr.decode('utf-8', errors='ignore')}")
                self.playing = False
                if self.ipc_socket and Path(self.ipc_socket).exists():
                    Path(self.ipc_socket).unlink(missing_ok=True)

            threading.Thread(target=monitor_player, daemon=True).start()
            return True
        except Exception as e:
            log(f"Error starting mpv: {str(e)}")
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
        log(f"Speed changed to {speed}x")

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
            log(f"Seek via IPC: {seconds}s")
            return True
        if self.current_episode:
            with self.position_lock:
                new_pos = max(0, min(self.position + seconds, self.duration or float('inf')))
                self.position = new_pos
                self.current_episode.position = new_pos
            log(f"Seek via restart: {seconds}s (maintaining position)")
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

class Litepop:
    """Main application class for litepop"""
    def __init__(self):
        self.config = Config()
        Path("/tmp/litepop_debug.log").write_text("")
        log("Starting Litepop application")
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
        """Syncs playback position to gPodder every 30 seconds"""
        while self.running:
            try:
                if self.player.playing and 0 <= self.current_index < len(self.queue):
                    episode = self.queue[self.current_index]
                    position = int(self.player.get_position())
                    duration = self.player.get_duration()
                    
                    if position > 0:  # Only sync if we have a meaningful position
                        action = {
                            "podcast": episode.podcast_url or episode.podcast_title,
                            "episode": episode.url,
                            "action": "play",
                            "timestamp": datetime.now().isoformat(),
                            "position": position,
                            "started": 0,
                            "total": int(duration) if duration and duration > 0 else 0,
                            "guid": episode.guid
                        }
                        log(f"Syncing position: {episode.title} at {position}s")
                        self.gpodder.upload_episode_actions([action])
                time.sleep(30)
            except Exception as e:
                log(f"Error in position sync: {str(e)}")
                time.sleep(60)

    def _sync_worker(self) -> None:
        """Handles periodic sync with gPodder"""
        sync_interval = int(self.config.get("gpodder", "sync_interval", "300"))
        log("Starting initial sync")
        self._sync_with_gpodder()
        self.initial_sync_done = True
        log("Initial sync completed")
        while self.running:
            time.sleep(sync_interval)
            if self.running:
                log("Starting periodic sync")
                self._sync_with_gpodder()

    def _playback_monitor(self) -> None:
        """Monitors playback status and auto-plays next episode"""
        consecutive_end_checks = 0  # Counter for consecutive end-of-file checks
        last_position = 0
        position_stuck_count = 0
    
        while self.running:
            try:
                if self.player.playing and self.current_index >= 0 and self.current_index < len(self.queue):
                    episode = self.queue[self.current_index]
                    duration = self.player.get_duration()
                    position = self.player.get_position()

                    # Multiple completion detection methods
                    episode_completed = False
                    completion_reason = ""

                    # Method 1: Position-based completion (99% threshold)
                    if duration > 0 and position > 0:
                        progress_percentage = (position / duration) * 100
                        if progress_percentage >= 99.0:
                            episode_completed = True
                            completion_reason = f"99% threshold reached ({progress_percentage:.1f}%)"

                    # Method 2: Position >= duration (original method)
                    if duration > 0 and position >= duration:
                        episode_completed = True
                        completion_reason = f"Position >= duration ({position:.1f}/{duration:.1f})"

                    # Method 3: Position stuck at end for multiple checks
                    if duration > 0 and position > 0:
                        if abs(position - last_position) < 0.5 and position >= (duration * 0.98):
                            position_stuck_count += 1
                            if position_stuck_count >= 4:  # 2 seconds of being stuck at ~end
                                episode_completed = True
                                completion_reason = f"Position stuck at end ({position:.1f}/{duration:.1f})"
                        else:
                            position_stuck_count = 0
                        last_position = position

                    # Method 4: Process terminated and we were near the end
                    if self.player.process and self.player.process.poll() is not None:
                        if duration > 0 and position >= (duration * 0.95):
                            episode_completed = True
                            completion_reason = f"Process ended near completion ({position:.1f}/{duration:.1f})"
                        else:
                            # Process ended unexpectedly (not at end)
                            log(f"MPV process terminated unexpectedly at {position:.1f}s of {duration:.1f}s")
                            self.player.playing = False
                            consecutive_end_checks = 0
                            position_stuck_count = 0
                            time.sleep(0.5)
                            continue

                    # If episode completed by any method
                    if episode_completed:
                        consecutive_end_checks += 1
                        log(f"Episode completion detected: {completion_reason} (check #{consecutive_end_checks})")
                    
                        # Require 2 consecutive checks to avoid false positives
                        if consecutive_end_checks >= 2:
                            log(f"Confirming episode completion: {episode.title}")
                            self.player.stop()  # Force stop mpv to release resources
                            self.mark_episode_completed(episode)
                            self.set_status_message(f"Completed: {episode.title}")

                            # Reset counters
                            consecutive_end_checks = 0
                            position_stuck_count = 0

                            # Auto-play next episode after a brief pause
                            if self.current_index + 1 < len(self.queue):
                                threading.Timer(1.5, lambda: self.play_selected(self.current_index + 1)).start()
                            else:
                                self.set_status_message("Queue completed!")
                    else:
                        consecutive_end_checks = 0

                # Check if the player process has terminated unexpectedly (when not near end)
                elif self.player.process and self.player.process.poll() is not None and self.player.playing:
                    log("MPV process terminated unexpectedly (not playing).")
                    self.player.playing = False
                    consecutive_end_checks = 0
                    position_stuck_count = 0

            except Exception as e:
                log(f"Error in playback monitor: {str(e)}")
                consecutive_end_checks = 0
                position_stuck_count = 0

            time.sleep(0.5)

    def _sync_with_gpodder(self) -> bool:
        """Syncs subscriptions and episode actions with gPodder"""
        try:
            if not self.config.get("gpodder", "username") or not self.config.get("gpodder", "password"):
                log("No credentials configured")
                return False
            
            log("Starting sync with gPodder server")
            
            # Upload any pending local actions first
            local_actions = self._get_pending_actions()
            if local_actions:
                log(f"Uploading {len(local_actions)} local actions")
                self.gpodder.upload_episode_actions(local_actions)
            
            # Get episode actions from server
            actions_data = self.gpodder.get_episode_actions()
            self._update_episode_actions_cache(actions_data.get("actions", []))
            
            # Get subscriptions
            subscriptions = self.gpodder.get_subscriptions()
            
            # If we already have subscriptions and get empty result, just refresh existing feeds
            if not subscriptions and self.subscriptions:
                log("No new subscriptions, refreshing existing feeds")
                for feed in self.subscriptions:
                    feed.fetch()
                self.last_sync = datetime.now()
                self._load_auto_queue()
                return True
            
            if not subscriptions:
                log("No subscriptions found")
                return False

            # Fetch new feeds in parallel
            new_feeds = []
            threads = []
            feed_lock = threading.Lock()

            def fetch_feed_threaded(sub_url: str) -> None:
                feed = PodcastFeed(sub_url)
                if feed.fetch():
                    with feed_lock:
                        new_feeds.append(feed)

            log(f"Fetching {len(subscriptions)} feeds")
            for sub_url in subscriptions:
                thread = threading.Thread(target=fetch_feed_threaded, args=(sub_url,))
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join()

            self.subscriptions = new_feeds
            self.last_sync = datetime.now()
            self._load_auto_queue()
            log(f"Sync completed: {len(new_feeds)} feeds loaded")
            return True
            
        except Exception as e:
            log(f"Error in sync: {str(e)}")
            return False

    def _update_episode_actions_cache(self, actions: List[Dict]) -> None:
        """Updates episode actions cache"""
        for action in actions:
            episode_url = action.get("episode")
            if not episode_url:
                continue
                
            # Initialize cache entry if doesn't exist
            if episode_url not in self.episode_actions_cache:  # CAMBIAR: litepop_instance. -> self.
                self.episode_actions_cache[episode_url] = {  # CAMBIAR: litepop_instance. -> self.
                    "progress": 0.0,
                    "position": 0,
                    "total": -1,
                    "server_completed": False,
                    "last_action": "unknown",
                    "last_timestamp": ""
                }
                
            cache_entry = self.episode_actions_cache[episode_url]  # CAMBIAR: litepop_instance. -> self.
            action_type = action.get("action", "").lower()
            timestamp = action.get("timestamp", "")
            
            # Update last action info
            cache_entry["last_action"] = action_type
            cache_entry["last_timestamp"] = timestamp
            
            if action_type == "play":
                position = int(action.get("position", 0))
                total = int(action.get("total", -1))
                
                # Only update if we have valid data
                if position > 0:
                    cache_entry["position"] = max(cache_entry["position"], position)
                    
                if total > 0:
                    cache_entry["total"] = total
                    # Calculate progress percentage
                    progress = (cache_entry["position"] / total) * 100
                    cache_entry["progress"] = min(progress, 100.0)
                    
                    # Mark as completed if >95%
                    if progress > 95.0:
                        cache_entry["server_completed"] = True
                        cache_entry["progress"] = 100.0
                        
            elif action_type == "download":
                # Download action means episode was completed
                cache_entry["server_completed"] = True
                cache_entry["progress"] = 100.0
                
            log(f"Updated cache for {episode_url}: pos={cache_entry['position']}, progress={cache_entry['progress']:.1f}%, completed={cache_entry['server_completed']}")

    def _get_episode_server_status(self, episode_url: str) -> Dict:
        """Gets episode status from cache"""
        default_status = {"progress": 0.0, "position": 0, "server_completed": False, "total": -1}
        status = self.episode_actions_cache.get(episode_url, default_status.copy())
        for key in default_status:
            if key not in status:
                status[key] = default_status[key]
        return status

    def _load_auto_queue(self) -> None:
        """Loads partially played episodes into queue"""
        log("Loading auto queue from episode actions")
        
        # Update existing queue items with server status
        for episode in self.queue:  # CAMBIAR: litepop_instance.queue -> self.queue
            server_status = self._get_episode_server_status(episode.url)  # CAMBIAR: litepop_instance._get... -> self._get...
            episode.server_completed = server_status["server_completed"]
            episode.position = max(episode.position, server_status["position"])
            episode.progress = max(episode.progress, server_status["progress"])

        # Find episodes to add to queue with more flexible criteria
        episodes_added = 0
        for episode_url, cache_data in self.episode_actions_cache.items():  # CAMBIAR: litepop_instance.episode... -> self.episode...
            progress = cache_data.get("progress", 0.0)
            position = cache_data.get("position", 0)
            is_completed = cache_data.get("server_completed", False)
            
            # Add episodes that:
            # 1. Have some progress (position > 30s) but aren't completed
            # 2. OR have 1-98% progress
            should_add = (
                (position > 30 and not is_completed) or 
                (1 <= progress < 98)
            ) and not any(ep.url == episode_url for ep in self.queue)  # CAMBIAR: litepop_instance.queue -> self.queue
            
            if should_add:
                # Find the episode in our feeds
                found_episode = None
                for feed in self.subscriptions:  # CAMBIAR: litepop_instance.subscriptions -> self.subscriptions
                    for episode_data in feed.episodes:
                        if episode_data["url"] == episode_url:
                            found_episode = episode_data
                            break
                    if found_episode:
                        break
                
                if found_episode:
                    episode = Episode(found_episode)  # REMOVER: "from litepop import Episode" - ya está definido arriba
                    episode.progress = progress
                    episode.position = position
                    episode.server_completed = is_completed
                    self.queue.append(episode)  # CAMBIAR: litepop_instance.queue -> self.queue
                    self.download_manager.download_episode(episode)  # CAMBIAR: litepop_instance.download_manager -> self.download_manager
                    episodes_added += 1
                    log(f"Added episode to queue: {episode.title} (progress: {progress:.1f}%, position: {position}s)")
                else:
                    log(f"Could not find episode in feeds: {episode_url}")
        
        if episodes_added > 0:
            log(f"Added {episodes_added} episodes to auto queue")
        else:
            log("No episodes added to auto queue")

    def _get_pending_actions(self) -> List[Dict]:
        """Gets pending episode actions for upload"""
        actions = []
        
        # Add current playing position
        if self.player.playing and 0 <= self.current_index < len(self.queue):
            episode = self.queue[self.current_index]
            current_position = self.player.get_position()
            if current_position > episode.position:
                episode.position = current_position
            actions.append({
                "podcast": episode.podcast_url or episode.podcast_title,
                "episode": episode.url,
                "action": "play",
                "timestamp": datetime.now().isoformat(),
                "position": int(episode.position),
                "started": 0,
                "total": int(episode.duration) if episode.duration else -1,
                "guid": episode.guid
            })

        # Add completed episodes
        for episode in self.queue:
            if episode.completed:
                actions.append({
                    "podcast": episode.podcast_url or episode.podcast_title,
                    "episode": episode.url,
                    "action": "download",
                    "timestamp": datetime.now().isoformat(),
                    "guid": episode.guid
                })
            elif episode.position > 0 and episode != (self.queue[self.current_index] if 0 <= self.current_index < len(self.queue) else None):
                # Add position updates for paused episodes
                actions.append({
                    "podcast": episode.podcast_url or episode.podcast_title,
                    "episode": episode.url,
                    "action": "play",
                    "timestamp": datetime.now().isoformat(),
                    "position": int(episode.position),
                    "started": 0,
                    "total": int(episode.duration) if episode.duration else -1,
                    "guid": episode.guid if episode.guid else ""
                })
        return actions

    def mark_episode_completed(self, episode: Episode) -> None:
        """Marks episode as completed and uploads status"""
        log(f"Marking episode as completed: {episode.title}")
        episode.completed = True
        episode.progress = 100.0
        episode.server_completed = True
    
        if episode.duration:
            episode.position = episode.duration
    
        # Upload both download and final play action
        actions = [
            {
                "podcast": episode.podcast_url or episode.podcast_title,
                "episode": episode.url,
                "action": "download",
                "timestamp": datetime.now().isoformat(),
                "guid": episode.guid
            },
            {
                "podcast": episode.podcast_url or episode.podcast_title,
                "episode": episode.url,
                "action": "play",
                "timestamp": datetime.now().isoformat(),
                "position": int(episode.position) if episode.position else int(episode.duration or 0),
                "started": 0,
                "total": int(episode.duration or -1),
                "guid": episode.guid
            }
        ]
    
        if self.gpodder.upload_episode_actions(actions):
            self.episode_actions_cache[episode.url] = {
                "progress": 100.0,
                "position": int(episode.position) if episode.position else int(episode.duration or 0),
                "total": int(episode.duration or -1),
                "server_completed": True
            }
        
        # Clean up the file after a short delay
        if episode.local_file:
            threading.Timer(1.0, lambda: self.download_manager.cleanup_file(episode.local_file)).start()

    def draw_header(self) -> None:
        """Draws header for UI"""
        height, width = self.stdscr.getmaxyx()
        backend_name = self.gpodder.backend.upper()
        header = f" litepop - Playback Queue ({backend_name}) "
        self.stdscr.attron(curses.color_pair(1))
        self.stdscr.addstr(0, 0, header.center(width))
        self.stdscr.attroff(curses.color_pair(1))

    def draw_queue(self, selected_index: int = 0) -> None:
        """Draws playback queue UI"""
        with self.ui_refresh_lock:
            self.stdscr.clear()
            self.draw_header()
            height, width = self.stdscr.getmaxyx()
            
            # Status line
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
            
            # Backend info
            backend_info = f"Backend: {self.gpodder.backend} | Device: {self.gpodder.device_id}"
            sync_status = f"Subscriptions: {len(self.subscriptions)} | Last sync: {self.last_sync.strftime('%H:%M') if self.last_sync else 'Never'}"
            self.stdscr.addstr(3, 2, f"{backend_info} | {sync_status}")
            
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
                    
                    # Status icons and colors
                    status_icon = " "
                    color_pair = 0
                    if episode.downloading:
                        status_icon, color_pair = "↓", 4
                    elif episode.completed or episode.server_completed:
                        status_icon, color_pair = "✓", 6
                    elif actual_index == self.current_index:
                        status_icon = "♪" if self.player.playing else "II"
                        color_pair = 3

                    if color_pair:
                        self.stdscr.attron(curses.color_pair(color_pair))
                    if actual_index == selected_index:
                        self.stdscr.attron(curses.color_pair(2))
                    
                    line = f"{status_icon} {title} - {episode.podcast_title} [{episode.progress:.1f}%]"
                    if actual_index == selected_index and actual_index != self.current_index:
                        line += " ←"
                    
                    self.stdscr.addstr(row, 2, line[:width-4])
                    
                    if actual_index == selected_index:
                        self.stdscr.attroff(curses.color_pair(2))
                    if color_pair:
                        self.stdscr.attroff(curses.color_pair(color_pair))

            # Help text
            help_lines = [
                f"SPACE:Play/Pause/Switch | ENTER:Next | ←→:±10s | d:Delete | D:Delete/Mark done | a:Add | s:Speed({self.player.speed}x) | R:Reset Progress",
                "c:Clear completed | r:Sync now | ESC/q:Exit | ♪:Playing II:Paused ←:Selected"
            ]
            for i, line in enumerate(help_lines):
                self.stdscr.addstr(height - 4 + i, 2, line[:width-4])
            
            # Log line
            if self.last_log_line:
                self.stdscr.attron(curses.color_pair(7))
                self.stdscr.addstr(height - 2, 2, f"Log: {self.last_log_line}"[:width-4])
                self.stdscr.attroff(curses.color_pair(7))
            
            # Status message
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
        
        # Collect all episodes not already in queue
        for feed in self.subscriptions:
            for episode in feed.episodes:
                if not any(ep.url == episode["url"] for ep in self.queue):
                    episode_obj = Episode(episode)
                    server_status = self._get_episode_server_status(episode_obj.url)
                    episode_obj.server_completed = server_status.get("server_completed", False)
                    episode_obj.progress = server_status.get("progress", 0.0)
                    episode_obj.position = server_status.get("position", 0)
                    all_episodes.append(episode_obj)

        # Sort by publication date
        all_episodes.sort(key=lambda ep: email.utils.parsedate_to_datetime(ep.pub_date).timestamp() if ep.pub_date else 0, reverse=True)
        
        # Group by date for display
        display_items = []
        current_date = None
        for episode in all_episodes:
            try:
                parsed_date = email.utils.parsedate_to_datetime(episode.pub_date) if episode.pub_date else None
                date_obj = parsed_date.date() if parsed_date else None
            except Exception as e:
                log(f"Error parsing pub_date for {episode.title}: {str(e)}")
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
                    dur_str = self.player.format_time(episode.duration) if episode.duration else "??:??:??"
                    line = f"  {title} - {episode.podcast_title} [{dur_str}] [{episode.progress:.1f}%]"
                    
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
                server_status = self._get_episode_server_status(episode.url)
                episode.position = server_status.get("position", 0)
                episode.progress = server_status.get("progress", 0.0)
                episode.server_completed = server_status.get("server_completed", False)
                self.queue.append(episode)
                self.download_manager.download_episode(episode)
                self.set_status_message(f"Added: {episode.title}")
                display_items.pop(selected)
                if selected >= len(display_items) and display_items:
                    selected = len(display_items) - 1
                elif not display_items:
                    return
            elif key == 27:  # ESC
                break

    def set_status_message(self, message: str, timeout: int = 3) -> None:
        """Sets temporary status message"""
        self.status_message = message
        self.status_timeout = time.time() + timeout

    def play_selected(self, index: int) -> bool:
        """Plays selected episode"""
        if 0 <= index < len(self.queue):
            if self.player.play(self.queue[index]):
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
        if episode.position > 0:
            action = {
                "podcast": episode.podcast_url or episode.podcast_title,
                "episode": episode.url,
                "action": "play",
                "timestamp": datetime.now().isoformat(),
                "position": int(episode.position),
                "started": 0,
                "total": int(episode.duration) if episode.duration else -1,
                "guid": episode.guid
            }
            self.gpodder.upload_episode_actions([action])

    def run(self) -> None:
        """Main application loop"""
        self.init_curses()
        
        # Start background threads
        for thread in self.threads:
            thread.start()
        
        # Wait for initial sync
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
                    selected_index = min(len(self.queue) - 1, selected_index + 1) if self.queue else 0
                elif key == ord(' '):  # Space - play/pause/switch
                    if self.queue and selected_index < len(self.queue):
                        if self.current_index == selected_index:
                            if self.player.playing:
                                self.player.stop()
                                self.set_status_message("Playback paused.")
                            else:
                                if self.queue[selected_index].local_file:
                                    self.player.play(self.queue[selected_index])
                                    self.set_status_message("Playback resumed.")
                                else:
                                    self.set_status_message("Episode not downloaded yet.")
                        else:
                            if self.queue[selected_index].local_file:
                                self.play_selected(selected_index)
                            else:
                                self.set_status_message("Episode not downloaded yet.")
                elif key in [curses.KEY_ENTER, 10, 13]:  # Enter - play next
                    self.play_next()
                elif key == curses.KEY_LEFT:  # Left arrow - seek back 10s
                    if self.player.seek(-10):
                        self.set_status_message("Seeked -10s.")
                elif key == curses.KEY_RIGHT:  # Right arrow - seek forward 10s
                    if self.player.seek(10):
                        self.set_status_message("Seeked +10s.")
                elif key == ord('d'):  # Delete episode
                    if self.queue and selected_index < len(self.queue):
                        if self.delete_episode(selected_index):
                            selected_index = min(selected_index, len(self.queue) - 1) if self.queue else 0
                elif key == ord('D'):  # Delete and mark as done
                    if self.queue and selected_index < len(self.queue):
                        if self.delete_and_mark_done(selected_index):
                            selected_index = min(selected_index, len(self.queue) - 1) if self.queue else 0
                elif key == ord('a'):  # Add episodes
                    self.add_episodes_screen()
                    selected_index = min(selected_index, len(self.queue) - 1) if self.queue else 0
                elif key == ord('s'):  # Change speed
                    speeds = {1.0: 1.5, 1.5: 1.75, 1.75: 2.0, 2.0: 0.5}
                    self.player.set_speed(speeds.get(self.player.speed, 1.0))
                    self.set_status_message(f"Speed set to {self.player.speed}x")
                elif key == ord('R'):  # Reset progress
                    if self.queue and selected_index < len(self.queue):
                        episode = self.queue[selected_index]
                        episode.position = 0
                        episode.completed = False
                        episode.server_completed = False
                        episode.progress = 0.0
                        self.set_status_message(f"Progress reset for: {episode.title}")
                        self._sync_episode_position(episode)
                elif key == ord('c'):  # Clear completed
                    self.clear_completed_episodes()
                elif key == ord('r'):  # Manual sync
                    self.set_status_message("Syncing with gPodder...")
                    if self._sync_with_gpodder():
                        self.set_status_message("Sync complete.")
                    else:
                        self.set_status_message("Sync failed.")
                elif key in [27, ord('q')]:  # ESC or q - quit
                    self.running = False
                    
        finally:
            # Cleanup
            log("Shutting down application")
            self.player.stop()
            
            # Upload any pending actions before exit
            pending = self._get_pending_actions()
            if pending:
                log(f"Uploading {len(pending)} pending actions before exit")
                self.gpodder.upload_episode_actions(pending)
            
            self.download_manager.cleanup_all_files()
            self.cleanup_curses()

if __name__ == "__main__":
    try:
        app = Litepop()
        app.run()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {str(e)}")
        log(f"Fatal error: {str(e)}")
