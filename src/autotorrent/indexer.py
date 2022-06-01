import logging
from fnmatch import fnmatch
from pathlib import Path

from .db import InsertTorrentFile
from .utils import get_root_of_unsplitable, is_unsplitable

logger = logging.getLogger(__name__)

INSERT_QUEUE_MAX_SIZE = 1000


class Indexer:
    def __init__(self, db, ignore_file_patterns=None):
        self.db = db
        self.ignore_file_patterns = ignore_file_patterns or []

    def scan_paths(self, paths, full_scan=True):
        if full_scan:
            self.db.truncate_files()
        paths = [Path(p) for p in paths]
        for path in paths:
            logger.info(f"Indexing path {path}")
            self._scan_path(path)
        self.db.commit()

    def _match_ignore_pattern(self, p):
        for ignore_file_pattern in self.ignore_file_patterns:
            if fnmatch(p.name, ignore_file_pattern):
                return True
        return False

    def _scan_path(self, path):
        files = []
        for p in path.iterdir():
            if p.is_dir():
                self._scan_path(p)
            elif p.is_file():
                if self._match_ignore_pattern(p):
                    continue
                files.append(p)
                self.db.insert_file_path(p)

        # TODO: probably not utf-8 problems resilient
        if is_unsplitable(files):  # TODO: prevent duplicate work (?)
            unsplitable_root = get_root_of_unsplitable(path)
            self.db.mark_unsplitable_root(unsplitable_root)

    def scan_clients(self, clients, full_scan=False, fast_scan=False):
        for name, client in clients.items():
            if full_scan:
                self.db.truncate_torrent_files(name)
            self._scan_client(name, client, not full_scan and fast_scan)
        self.db.commit()

    def _scan_client(self, client_name, client, fast_scan):
        torrents = client.list()
        insert_queue = []
        for torrent in torrents:
            _, current_download_path = self.db.get_torrent_file_info(
                client_name, torrent.infohash
            )
            if fast_scan and current_download_path is not None:
                logger.debug(
                    f"torrent:{torrent!r} client:{client!r} Skip indexing because it is already there and fast-scan is enabled"
                )
                continue

            download_path = client.get_download_path(torrent.infohash)
            if str(download_path) == current_download_path:
                logger.debug(
                    f"torrent:{torrent!r} client:{client!r} Skip indexing because download path not changed"
                )
                continue

            files = client.get_files(torrent.infohash)
            if not files:
                logger.debug("No files found, not loaded")
            paths = []
            for f in files:
                f_path = download_path / f.path
                paths.append((str(f_path), f.size))
                f_path_resolved = f_path.resolve()
                if f_path_resolved != f_path:
                    paths.append((str(f_path_resolved), f.size))
            insert_queue.append(
                InsertTorrentFile(torrent.infohash, torrent.name, download_path, paths)
            )
            if len(insert_queue) > INSERT_QUEUE_MAX_SIZE:
                self.db.insert_torrent_files_paths(client_name, insert_queue)
                insert_queue = []
        if insert_queue:
            self.db.insert_torrent_files_paths(client_name, insert_queue)

        self.db.remove_non_existing_infohashes(
            client_name, [torrent.infohash for torrent in torrents]
        )
