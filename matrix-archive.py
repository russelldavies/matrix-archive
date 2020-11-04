#!/usr/bin/env python3

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
import asyncio
import getpass
import itertools
import os
import sys
import yaml


DEVICE_NAME = "matrix-archive"


def mkdir(path):
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    return path


OUTPUT_DIR = mkdir(sys.argv[1] if 1 < len(sys.argv) else ".")


async def create_client() -> AsyncClient:
    homeserver = "https://matrix-client.matrix.org"
    homeserver = input(f"Enter URL of your homeserver: [{homeserver}] ") or homeserver
    user_id = input(f"Enter your full user ID (e.g. @user:matrix.org): ")
    password = getpass.getpass()
    client = AsyncClient(
        homeserver=homeserver,
        user=user_id,
        config=AsyncClientConfig(store=store.SqliteMemoryStore),
    )
    await client.login(password, DEVICE_NAME)
    client.load_store()
    room_keys_path = input("Enter full path to room E2E keys: ")
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
        await output_file.write(serialize_event(dict(type="media", src=filename,)))
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
    return response.body


async def fetch_room_events(
    client: AsyncClient,
    start_token: str,
    room: MatrixRoom,
    direction: MessageDirection,
) -> list:
    is_valid_event = lambda e: isinstance(
        e, (RoomMessageFormatted, RoomMessageMedia, RoomEncryptedMedia, RedactedEvent,)
    )
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


async def main() -> None:
    try:
        client = await create_client()
        while True:
            sync_resp = await client.sync(
                full_state=True, sync_filter={"room": {"timeline": {"limit": 1}}}
            )
            room = await select_room(client)
            print("Fetching room messages and writing to disk...")
            start_token = sync_resp.rooms.join[room.room_id].timeline.prev_batch
            # Generally, it should only be necessary to fetch back events but,
            # sometimes depending on the sync, front events need to be fetched
            # as well.
            fetch_room_events_ = partial(fetch_room_events, client, start_token, room)
            async with aiofiles.open(
                f"{OUTPUT_DIR}/{room.display_name}_{room.room_id}.yaml", "w"
            ) as f:
                for events in [
                    reversed(await fetch_room_events_(MessageDirection.back)),
                    await fetch_room_events_(MessageDirection.front),
                ]:
                    for event in events:
                        try:
                            await write_event(client, room, f, event)
                        except exceptions.EncryptionError as e:
                            print(e)
            await save_avatars(client, room)
            print("Successfully wrote all events to disk.")
    except KeyboardInterrupt:
        sys.exit(1)
    finally:
        await client.logout()
        await client.close()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
