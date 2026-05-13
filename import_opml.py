#!/usr/bin/env python3
"""
import_opml.py - Importar suscripciones OPML a FilePodSync

Uso:
    python import_opml.py antennapod-feeds-2026-05-13.opml
    python import_opml.py --dry-run antennapod-feeds-2026-05-13.opml  # Solo muestra lo que haría

Este script:
1. Lee ~/.config/litepop.conf para obtener sync_dir de FilePodSync
2. Parsea el archivo OPML y extrae feeds RSS
3. Convierte al formato feeds.json de FilePodSync
4. Escribe atómicamente preservando datos existentes
"""
import argparse
import configparser
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse, urlunparse

# ─────────────────────────────────────────────────────────────
# CONSTANTES Y CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────
SCHEMA_VERSION = "1.3.0"
CONFIG_PATH = Path.home() / ".config" / "litepop.conf"


def get_utc_ms() -> int:
    """Return current UTC time in milliseconds since Unix epoch."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _normalize_url(url: str) -> str:
    """
    Normaliza URL de feed para usar como clave estable.
    Reglas:
    1. Lowercase scheme y host
    2. Remover puertos por defecto (:80 http, :443 https)
    3. Decodificar percent-encoding en path
    4. Remover trailing slash (salvo que path sea solo "/")
    5. Preservar query string y fragment
    """
    if not url:
        return ""
    p = urlparse(url.strip())
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    
    # Remover puertos por defecto
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host
    
    # Decodificar path y remover trailing slash
    path = unquote(p.path)
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    
    return urlunparse((scheme, netloc, path, "", p.query, p.fragment))


def _atomic_write(path: Path, data: dict) -> None:
    """Escribe JSON atómicamente: primero a .tmp, luego rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8"
    )
    # Rename atómico (POSIX) o best-effort (Windows)
    import os
    os.replace(str(tmp), str(path))


def _load_json_safe(path: Path, fallback: Optional[dict] = None) -> dict:
    """Carga JSON con manejo seguro de errores."""
    if not path.exists():
        return fallback or {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️ Warning: Could not load {path}: {exc}")
        return fallback or {}


def _get_device_id(sync_dir: Path) -> str:
    """Obtiene o genera el device_id para FilePodSync."""
    id_file = sync_dir / ".fps_device_id"
    if id_file.exists():
        raw = id_file.read_text(encoding="utf-8").strip()
        # Limpiar quotes si fue escrito con bug de v1.2
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        return raw
    
    # Generar nuevo UUID
    import uuid
    new_id = str(uuid.uuid4())
    id_file.write_text(new_id, encoding="utf-8")  # Plain text, NO json.dumps
    print(f"ℹ️ Generated new device ID: {new_id}")
    return new_id


def parse_opml(opml_path: Path) -> List[Dict[str, str]]:
    """
    Parsea archivo OPML y extrae lista de feeds.
    Retorna: lista de dicts con {'url': ..., 'title': ..., 'htmlUrl': ...}
    """
    feeds = []
    
    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"❌ Error parsing OPML: {e}")
        return feeds
    except FileNotFoundError:
        print(f"❌ File not found: {opml_path}")
        return feeds
    
    # Buscar todos los <outline> con xmlUrl (feeds RSS)
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl", "").strip()
        if not xml_url:
            continue
        
        title = outline.get("text", outline.get("title", "")).strip()
        html_url = outline.get("htmlUrl", "").strip()
        
        feeds.append({
            "url": xml_url,
            "title": title or xml_url,
            "htmlUrl": html_url
        })
    
    print(f"✅ Parsed {len(feeds)} feeds from {opml_path.name}")
    return feeds


def convert_to_filepodsync_format(
    feeds: List[Dict[str, str]], 
    device_id: str,
    existing_feeds: Optional[Dict] = None
) -> Dict:
    """
    Convierte lista de feeds OPML al formato feeds.json de FilePodSync.
    
    - Preserva feeds existentes que no estén en el OPML (merge, no reemplazo)
    - Actualiza feeds existentes con nuevo título si cambia
    - Marca nuevos feeds como "active" y "healthy"
    """
    now = get_utc_ms()
    
    # Estructura base del archivo
    result = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now,
        "updated_by": device_id,
        "feeds": existing_feeds or {}
    }
    
    for feed in feeds:
        norm_url = _normalize_url(feed["url"])
        if not norm_url:
            print(f"⚠️ Skipping invalid URL: {feed['url']}")
            continue
        
        # Si ya existe, actualizar título pero preservar metadata
        if norm_url in result["feeds"]:
            existing = result["feeds"][norm_url]
            if feed["title"] and existing.get("title") != feed["title"]:
                print(f"🔄 Updating title for: {norm_url}")
                existing["title"] = feed["title"]
                existing["updated_at"] = now
                existing["updated_by"] = device_id
        else:
            # Nuevo feed
            result["feeds"][norm_url] = {
                "url": norm_url,
                "title": feed["title"],
                "status": "active",
                "health_status": "healthy",
                "last_check": 0,
                "error_count": 0,
                "added_by": device_id,
                "added_at": now,
                "updated_by": device_id,
                "updated_at": now,
                "custom": {
                    "htmlUrl": feed.get("htmlUrl", "")
                } if feed.get("htmlUrl") else {}
            }
            print(f"➕ Added: {feed['title']}")
    
    return result


def get_sync_dir_from_config() -> Optional[Path]:
    """Lee sync_dir desde litepop.conf sección [filepodsync]."""
    if not CONFIG_PATH.exists():
        print(f"❌ Config file not found: {CONFIG_PATH}")
        return None
    
    cfg = configparser.ConfigParser()
    
    # Intentar múltiples encodings
    for enc in ["utf-8", "iso-8859-1", "cp1252"]:
        try:
            cfg.read(CONFIG_PATH, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        print("❌ Could not read config file (encoding issue)")
        return None
    
    # Buscar sync_dir en orden de preferencia
    sync_dir = None
    if cfg.has_section("filepodsync"):
        sync_dir = cfg.get("filepodsync", "sync_dir", fallback=None)
    
    if not sync_dir:
        print("❌ [filepodsync] sync_dir not found in config")
        print("💡 Add this to ~/.config/litepop.conf:")
        print("   [filepodsync]")
        print("   enabled = true")
        print("   sync_dir = /ruta/a/tu/carpeta/sync/")
        return None
    
    sync_path = Path(sync_dir).expanduser().resolve()
    if not sync_path.exists():
        print(f"❌ Sync directory does not exist: {sync_path}")
        return None
    
    return sync_path


def main():
    parser = argparse.ArgumentParser(
        description="Import OPML subscriptions to FilePodSync feeds.json",
        epilog="Example: python import_opml.py antennapod-feeds-2026-05-13.opml"
    )
    parser.add_argument("opml_file", help="Path to OPML file")
    parser.add_argument("--dry-run", action="store_true", 
                       help="Show what would be done without writing")
    parser.add_argument("--force", action="store_true",
                       help="Replace existing feeds.json instead of merging")
    args = parser.parse_args()
    
    print(f"🔍 Importing OPML to FilePodSync")
    print(f"   OPML file: {args.opml_file}")
    
    # 1. Obtener sync_dir desde config
    sync_dir = get_sync_dir_from_config()
    if not sync_dir:
        sys.exit(1)
    print(f"   Sync dir: {sync_dir}")
    
    # 2. Parsear OPML
    feeds = parse_opml(Path(args.opml_file))
    if not feeds:
        print("❌ No feeds found in OPML")
        sys.exit(1)
    
    # 3. Obtener device_id
    device_id = _get_device_id(sync_dir)
    
    # 4. Cargar feeds.json existente (si merge mode)
    feeds_json_path = sync_dir / "feeds.json"
    existing_feeds = None
    if not args.force and feeds_json_path.exists():
        existing_data = _load_json_safe(feeds_json_path)
        existing_feeds = existing_data.get("feeds", {})
        print(f"📋 Merging with {len(existing_feeds)} existing feeds")
    
    # 5. Convertir
    result = convert_to_filepodsync_format(feeds, device_id, existing_feeds)
    
    # 6. Mostrar resumen
    new_count = sum(1 for f in feeds if _normalize_url(f["url"]) not in (existing_feeds or {}))
    print(f"\n📊 Summary:")
    print(f"   Total feeds in OPML: {len(feeds)}")
    print(f"   New feeds added: {new_count}")
    print(f"   Existing feeds preserved: {len(existing_feeds or {}) - new_count if existing_feeds else 0}")
    print(f"   Total in feeds.json: {len(result['feeds'])}")
    
    if args.dry_run:
        print(f"\n🧪 DRY RUN - No files were written")
        return
    
    # 7. Escribir atómicamente
    try:
        _atomic_write(feeds_json_path, result)
        print(f"\n✅ Successfully wrote {feeds_json_path}")
        
        # Forzar sync inicial si FilePodSync está disponible
        try:
            from filepodsync import FilePodSync
            print("🔄 Triggering FilePodSync sync to propagate changes...")
            fps = FilePodSync(
                sync_dir=str(sync_dir),
                device_name="import_opml",
                platform="python",
                client="import_script"
            )
            fps.sync(force=True)
            fps.shutdown()
            print("✅ Sync complete - changes propagated to other devices")
        except ImportError:
            print("ℹ️ FilePodSync library not available - changes will sync when litepop runs")
        
    except Exception as e:
        print(f"❌ Error writing feeds.json: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()