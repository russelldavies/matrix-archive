#!/usr/bin/env python3

"""matrix-archive

Archive Matrix room messages. Creates a YAML log of all room
messages, including media.

Use the unattended batch mode to fetch everything in one go without
having to type anything during script execution. You can set all
the necessary values with arguments to your command call.

If you don't want to put your passwords in the command call, you
can still set the default values for homeserver, user ID and room
keys path already to have them suggested to you during interactive
execution. Rooms that you specify in the command call will be
automatically fetched before prompting for further input.

Example calls:

./matrix-archive.py
    Run program in interactive mode.

./matrix-archive.py backups
    Set output folder for selected rooms.

./matrix-archive.py --batch --user '@user:matrix.org' --userpass secret --keys element-keys.txt --keyspass secret
    Use unattended batch mode to login.

./matrix-archive.py --room '!Abcdefghijklmnopqr:matrix.org'
    Automatically fetch a room.

./matrix-archive.py --room '!Abcdefghijklmnopqr:matrix.org' --room '!Bcdefghijklmnopqrs:matrix.org'
    Automatically fetch two rooms.

./matrix-archive.py --roomregex '.*:matrix.org'
    Automatically fetch every rooms which matches a regex pattern.

./matrix-archive.py --all-rooms
    Automatically fetch all available rooms.

"""


from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    MessageDirection,
    RedactedEvent,
    RoomEncryptedMedia,
    RoomMessage,
    RoomMessageFormatted,
    RoomMessageMedia,
    crypto,
    store,
    exceptions
)
from functools import partial
from typing import Union, TextIO
from urllib.parse import urlparse
import aiofiles
import argparse
import asyncio
import getpass
import itertools
import os
import re
import sys
import yaml
import json


DEVICE_NAME = "matrix-archive"


def parse_args():
    """Parse arguments from command line call"""

    parser = argparse.ArgumentParser(
        description=__doc__,
        add_help=False,  # Use individual setting below instead
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "folder",
        metavar="FOLDER",
        default=".",
        nargs="?",  # Make positional argument optional
        help="""Set output folder
             """,
    )
    parser.add_argument(
        "--help",
        action="help",
        help="""Show this help message and exit
             """,
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="""Use unattended batch mode
             """,
    )
    parser.add_argument(
        "--server",
        metavar="HOST",
        default="https://matrix-client.matrix.org",
        help="""Set default Matrix homeserver
             """,
    )
    parser.add_argument(
        "--user",
        metavar="USER_ID",
        default="@user:matrix.org",
        help="""Set default user ID
             """,
    )
    parser.add_argument(
        "--userpass",
        metavar="PASSWORD",
        help="""Set default user password
             """,
    )
    parser.add_argument(
        "--keys",
        metavar="FILENAME",
        default="element-keys.txt",
        help="""Set default path to room E2E keys
             """,
    )
    parser.add_argument(
        "--keyspass",
        metavar="PASSWORD",
        help="""Set default passphrase for room E2E keys
             """,
    )
    parser.add_argument(
        "--room",
        metavar="ROOM_ID",
        default=[],
        action="append",
        help="""Add room to list of automatically fetched rooms
             """,
    )
    parser.add_argument(
        "--roomregex",
        metavar="PATTERN",
        default=[],
        action="append",
        help="""Same as --room but by regex pattern
             """,
    )
    parser.add_argument(
        "--all-rooms",
        action="store_true",
        help="""Select all rooms
             """,
    )
    parser.add_argument(
        "--no-media",
        action="store_true",
        help="""Don't download media
             """,
    )
    return parser.parse_args()


def mkdir(path):
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    return path


async def create_client() -> AsyncClient:
    homeserver = ARGS.server
    user_id = ARGS.user
    password = ARGS.userpass
    if not ARGS.batch:
        homeserver = input(f"Enter URL of your homeserver: [{homeserver}] ") or homeserver
        user_id = input(f"Enter your full user ID: [{user_id}] ") or user_id
        password = getpass.getpass()
    client = AsyncClient(
        homeserver=homeserver,
        user=user_id,
        config=AsyncClientConfig(store=store.SqliteMemoryStore),
    )
    await client.login(password, DEVICE_NAME)
    client.load_store()
    room_keys_path = ARGS.keys
    room_keys_password = ARGS.keyspass
    if not ARGS.batch:
        room_keys_path = input(f"Enter full path to room E2E keys: [{room_keys_path}] ") or room_keys_path
        room_keys_password = getpass.getpass("Room keys password: ")
    print("Importing keys. This may take a while...")
    await client.import_keys(room_keys_path, room_keys_password)
    return client


async def select_room(client: AsyncClient) -> MatrixRoom:
    print("\nList of joined rooms (room id, display name):")
    for room_id, room in client.rooms.items():
        print(f"{room_id}, {room.display_name}")
    room_id = input(f"Enter room id: ")
    return client.rooms[room_id]


def choose_filename(filename):
    start, ext = os.path.splitext(filename)
    for i in itertools.count(1):
        if not os.path.exists(filename):
            break
        filename = f"{start}({i}){ext}"
    return filename


async def write_event(
    client: AsyncClient, room: MatrixRoom, output_file: TextIO, event: RoomMessage
) -> None:
    if not ARGS.no_media:
        media_dir = mkdir(f"{OUTPUT_DIR}/{room.display_name}_{room.room_id}_media")
    sender_name = f"<{event.sender}>"
    if event.sender in room.users:
        # If user is still present in room, include current nickname
        sender_name = f"{room.users[event.sender].display_name} {sender_name}"
    serialize_event = lambda event_payload: yaml.dump(
        [
            {
                **dict(
                    sender_id=event.sender,
                    sender_name=sender_name,
                    timestamp=event.server_timestamp,
                ),
                **event_payload,
            }
        ]
    )

    if isinstance(event, RoomMessageFormatted):
        await output_file.write(serialize_event(dict(type="text", body=event.body,)))
    elif isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
        media_data = await download_mxc(client, event.url)
        filename = choose_filename(f"{media_dir}/{event.body}")
        async with aiofiles.open(filename, "wb") as f:
            try:
                await f.write(
                    crypto.attachments.decrypt_attachment(
                        media_data,
                        event.source["content"]["file"]["key"]["k"],
                        event.source["content"]["file"]["hashes"]["sha256"],
                        event.source["content"]["file"]["iv"],
                    )
                )
            except KeyError:  # EAFP: Unencrypted media produces KeyError
                await f.write(media_data)
            # Set atime and mtime of file to event timestamp
            os.utime(filename, ns=((event.server_timestamp * 1000000,) * 2))
        await output_file.write(serialize_event(dict(type="media", src="." + filename[len(OUTPUT_DIR):],)))
    elif isinstance(event, RedactedEvent):
        await output_file.write(serialize_event(dict(type="redacted",)))


async def save_avatars(client: AsyncClient, room: MatrixRoom) -> None:
    avatar_dir = mkdir(f"{OUTPUT_DIR}/{room.display_name}_{room.room_id}_avatars")
    for user in room.users.values():
        if user.avatar_url:
            async with aiofiles.open(f"{avatar_dir}/{user.user_id}", "wb") as f:
                await f.write(await download_mxc(client, user.avatar_url))


async def download_mxc(client: AsyncClient, url: str):
    mxc = urlparse(url)
    response = await client.download(mxc.netloc, mxc.path.strip("/"))
    if hasattr(response, "body"):
        return response.body
    else:
        return b''


def is_valid_event(event):
    events = (RoomMessageFormatted, RedactedEvent)
    if not ARGS.no_media:
        events += (RoomMessageMedia, RoomEncryptedMedia)
    return isinstance(event, events)


async def fetch_room_events(
    client: AsyncClient,
    start_token: str,
    room: MatrixRoom,
    direction: MessageDirection,
) -> list:
    events = []
    while True:
        response = await client.room_messages(
            room.room_id, start_token, limit=1000, direction=direction
        )
        if len(response.chunk) == 0:
            break
        events.extend(event for event in response.chunk if is_valid_event(event))
        start_token = response.end
    return events


async def write_room_events(client, room):
    print(f"Fetching {room.room_id} room messages and writing to disk...")
    sync_resp = await client.sync(
        full_state=True, sync_filter={"room": {"timeline": {"limit": 1}}}
    )
    start_token = sync_resp.rooms.join[room.room_id].timeline.prev_batch
    # Generally, it should only be necessary to fetch back events but,
    # sometimes depending on the sync, front events need to be fetched
    # as well.
    fetch_room_events_ = partial(fetch_room_events, client, start_token, room)
    async with aiofiles.open(
        f"{OUTPUT_DIR}/{room.display_name}_{room.room_id}.json", "w"
    ) as f_json:
        for events in [
            reversed(await fetch_room_events_(MessageDirection.back)),
            await fetch_room_events_(MessageDirection.front),
        ]:
            events_parsed = []
            for event in events:
                try:
                    if not ARGS.no_media:
                        media_dir = mkdir(f"{OUTPUT_DIR}/{room.display_name}_{room.room_id}_media")

                    # add additional information to the message source
                    sender_name = f"<{event.sender}>"
                    if event.sender in room.users:
                        # If user is still present in room, include current nickname
                        sender_name = f"{room.users[event.sender].display_name} {sender_name}"
                        event.source["_sender_name"] = sender_name

                    # download media if necessary
                    if isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
                        media_data = await download_mxc(client, event.url)
                        filename = choose_filename(f"{media_dir}/{event.body}")
                        event.source["_media_location"] = filename
                        async with aiofiles.open(filename, "wb") as f_media:
                            try:
                                await f_media.write(
                                    crypto.attachments.decrypt_attachment(
                                        media_data,
                                        event.source["content"]["file"]["key"]["k"],
                                        event.source["content"]["file"]["hashes"]["sha256"],
                                        event.source["content"]["file"]["iv"],
                                    )
                                )
                            except KeyError:  # EAFP: Unencrypted media produces KeyError
                                await f_media.write(media_data)
                            # Set atime and mtime of file to event timestamp
                            os.utime(filename, ns=((event.server_timestamp * 1000000,) * 2))

                    # write out the processed message source
                    events_parsed.append(event.source)
                except exceptions.EncryptionError as e:
                    print(e, file=sys.stderr)
            # serialise message array
            await f_json.write(json.dumps(events_parsed, indent=4))
    await save_avatars(client, room)
    print("Successfully wrote all room events to disk.")


async def main() -> None:
    try:
        client = await create_client()
        await client.sync(
            full_state=True,
            # Limit fetch of room events as they will be fetched later
            sync_filter={"room": {"timeline": {"limit": 1}}})
        for room_id, room in client.rooms.items():
            # Iterate over rooms to see if a room has been selected to
            # be automatically fetched
            if room_id in ARGS.room or any(re.match(pattern, room_id) for pattern in ARGS.roomregex):
                print(f"Selected room: {room_id}")
                await write_room_events(client, room)
        if ARGS.batch:
            # If the program is running in unattended batch mode,
            # then we can quit at this point
            raise SystemExit
        else:
            while True:
                room = await select_room(client)
                await write_room_events(client, room)
    except KeyboardInterrupt:
        sys.exit(1)
    finally:
        await client.logout()
        await client.close()


if __name__ == "__main__":
    ARGS = parse_args()
    if ARGS.all_rooms:
        # Select all rooms by adding a regex pattern which matches every string
        ARGS.roomregex.append(".*")
    OUTPUT_DIR = mkdir(ARGS.folder)
    asyncio.get_event_loop().run_until_complete(main())
