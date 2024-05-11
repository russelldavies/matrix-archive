#!/usr/bin/env python3

from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    MessageDirection,
    RedactedEvent,
    RoomEncryptedMedia,
    RoomMessageFormatted,
    RoomMessageMedia,
    crypto,
    store,
    exceptions,
)
from functools import partial
from urllib.parse import urlparse
import aiofiles
import argparse
import asyncio
import itertools
import logging
import os
import re
import yaml
import json

logger = logging.getLogger()
logger.addHandler(logging.StreamHandler())
SCRIPTDIR = os.path.dirname(os.path.realpath(__file__))


def parse_args():
    """Parse arguments from command line call"""

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "config",
        default=os.path.join(SCRIPTDIR, "config.yml"),
        type="str",
        help="Path to config file",
    )
    return parser.parse_args()


class MatrixBackup(object):
    async def __init__(self, config: dict) -> None:
        self._client = AsyncClient(
            homeserver=config["server"]["host"],
            user=config["server"]["user"],
            config=AsyncClientConfig(store=store.SqliteMemoryStore),
        )
        await self._client.login(
            config["server"]["password"], config["server"]["device_name"]
        )
        self._client.load_store()
        logger.info("Importing keys. This may take a while...")
        await self._client.import_keys(
            config["server"]["keys_file"], config["server"]["keys_password"]
        )
        self.output = config["root_folder"]
        self.no_media = config.get("no_media", False)

    async def shutdown(self) -> None:
        await self._client.logout()
        await self._client.close()

    def choose_filename(self, filename):
        start, ext = os.path.splitext(filename)
        for i in itertools.count(1):
            if not os.path.exists(filename):
                break
            filename = f"{start}({i}){ext}"
        return filename

    async def save_avatars(self, room: MatrixRoom) -> None:
        avatar_dir = os.makedirs(
            f"{self.output}/{room.display_name}_{room.room_id}_avatars", exist_ok=True
        )
        for user in room.users.values():
            if user.avatar_url:
                async with aiofiles.open(f"{avatar_dir}/{user.user_id}", "wb") as f:
                    await f.write(await self.download_mxc(user.avatar_url))

    async def download_mxc(self, url: str):
        mxc = urlparse(url)
        response = await self._client.download(mxc.netloc, mxc.path.strip("/"))
        if hasattr(response, "body"):
            return response.body
        else:
            return b""

    def _is_valid_event(self, event):
        events = (RoomMessageFormatted, RedactedEvent)
        if not self.no_media:
            events += (RoomMessageMedia, RoomEncryptedMedia)
        return isinstance(event, events)

    async def fetch_room_events(
        self,
        start_token: str,
        room: MatrixRoom,
        direction: MessageDirection,
    ) -> list:
        events = []
        while True:
            response = await self._client.room_messages(
                room.room_id, start_token, limit=1000, direction=direction
            )
            if len(response.chunk) == 0:
                break
            events.extend(
                event for event in response.chunk if self._is_valid_event(event)
            )
            start_token = response.end
        return events

    async def write_room_events(self, room) -> None:
        logger.info("Fetching %s room messages and writing to disk...", room.room_id)
        sync_resp = await self._client.sync(
            full_state=True, sync_filter={"room": {"timeline": {"limit": 1}}}
        )
        start_token = sync_resp.rooms.join[room.room_id].timeline.prev_batch
        # Generally, it should only be necessary to fetch back events but,
        # sometimes depending on the sync, front events need to be fetched
        # as well.
        fetch_room_events_ = partial(self.fetch_room_events, start_token, room)
        async with aiofiles.open(
            f"{self.output}/{room.display_name}_{room.room_id}.json", "w"
        ) as f_json:
            for events in [
                reversed(await fetch_room_events_(MessageDirection.back)),
                await fetch_room_events_(MessageDirection.front),
            ]:
                events_parsed = []
                for event in events:
                    try:
                        if not self.no_media:
                            media_dir = os.makedirs(
                                f"{self.output}/{room.display_name}_{room.room_id}_media",
                                exist_ok=True,
                            )

                        # add additional information to the message source
                        sender_name = f"<{event.sender}>"
                        if event.sender in room.users:
                            # If user is still present in room, include current nickname
                            sender_name = (
                                f"{room.users[event.sender].display_name} {sender_name}"
                            )
                            event.source["_sender_name"] = sender_name

                        # download media if necessary
                        if isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
                            media_data = await self.download_mxc(event.url)
                            filename = self.choose_filename(f"{media_dir}/{event.body}")
                            event.source["_file_path"] = filename
                            async with aiofiles.open(filename, "wb") as f_media:
                                try:
                                    await f_media.write(
                                        crypto.attachments.decrypt_attachment(
                                            media_data,
                                            event.source["content"]["file"]["key"]["k"],
                                            event.source["content"]["file"]["hashes"][
                                                "sha256"
                                            ],
                                            event.source["content"]["file"]["iv"],
                                        )
                                    )
                                except (
                                    KeyError
                                ):  # EAFP: Unencrypted media produces KeyError
                                    await f_media.write(media_data)
                                # Set atime and mtime of file to event timestamp
                                os.utime(
                                    filename,
                                    ns=((event.server_timestamp * 1000000,) * 2),
                                )

                        # write out the processed message source
                        events_parsed.append(event.source)
                    except exceptions.EncryptionError as e:
                        logger.error(e)
                # serialise message array
                await json.dump(events_parsed, f_json, indent=4)
        await self.save_avatars(room)
        logger.info("Successfully wrote all room events to disk.")

    async def backup_rooms(self) -> None:

        try:
            await self._client.sync(
                full_state=True,
                # Limit fetch of room events as they will be fetched later
                sync_filter={"room": {"timeline": {"limit": 1}}},
            )
            for room_id, room in self._client.rooms.items():
                # Iterate over rooms to see if a room has been selected to
                # be automatically fetched
                if room_id in config["room_list"] or any(
                    re.match(pattern, room_id) for pattern in config["room_regex"]
                ):
                    logger.info("Selected room: %s", room_id)
                    await self._client.write_room_events(room)
        except KeyboardInterrupt:
            exit(1)
        finally:
            self._client.shutdown()


async def main(config: dict) -> None:
    client = await MatrixBackup(config)
    client.backup_rooms()


def load_config(config_file: str) -> dict:
    with open(config_file, "r") as f_in:
        config = yaml.safe_load(f_in.read())
    if not config["root_folder"].startswith("/"):
        config["root_folder"] = os.path.join(SCRIPTDIR, config["root_folder"])
    os.makedirs(config["root_folder"], exist_ok=True)
    if not config["server"]["keys_file"].startswith("/"):
        config["server"]["keys_file"] = os.path.join(
            SCRIPTDIR, config["server"]["keys_file"]
        )
    return config


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    asyncio.get_event_loop().run_until_complete(main(config))
