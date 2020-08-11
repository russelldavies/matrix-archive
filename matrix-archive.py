#!/usr/bin/env python3

import asyncio
import getpass
from urllib.parse import urlparse
from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    MessageDirection,
    ProfileGetAvatarResponse,
    RedactedEvent,
    RoomEncryptedMedia,
    RoomMessageFormatted,
    RoomMessageMedia,
    RoomMessagesError,
    SyncResponse,
    UnknownEvent,
    crypto,
    store,
)
from typing import Union


DEVICE_NAME = "matrix-archive"


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
    print("Importing keys, this may take a while...")
    await client.import_keys(room_keys_path, room_keys_password)
    return client


async def select_room(client: AsyncClient) -> MatrixRoom:
    print("List of joined rooms (room id, display name):")
    for room_id, room in client.rooms.items():
        print(f"{room_id}, {room.display_name}")
    room_id = input(f"Enter room id: ")
    return client.rooms[room_id]


async def fetch_room_events(
    client: AsyncClient,
    sync_resp: SyncResponse,
    room: MatrixRoom,
    direction: MessageDirection,
) -> list:
    start_token = sync_resp.rooms.join[room.room_id].timeline.prev_batch
    events = []
    while True:
        response = await client.room_messages(
            room.room_id, start_token, limit=1000, direction=direction
        )
        if isinstance(response, RoomMessagesError):
            break
        start_token = response.end
        if len(response.chunk) == 0:
            break
        events.extend(response.chunk)
    return events


async def save_media(
    client: AsyncClient, event: Union[RoomMessageMedia, RoomEncryptedMedia]
) -> None:
    mxc = urlparse(event.url)
    response = await client.download(mxc.netloc, mxc.path.strip("/"))
    with open(event.body, "wb") as f:
        f.write(
            crypto.attachments.decrypt_attachment(
                response.body,
                event.source["content"]["file"]["key"]["k"],
                event.source["content"]["file"]["hashes"]["sha256"],
                event.source["content"]["file"]["iv"],
            )
        )


async def room_messages(
    client: AsyncClient, sync_resp: SyncResponse, room: MatrixRoom
) -> None:
    back_events = await fetch_room_events(
        client, sync_resp, room, MessageDirection.back
    )
    front_events = await fetch_room_events(
        client, sync_resp, room, MessageDirection.front
    )

    for event in back_events[::-1] + front_events:
        if isinstance(event, RoomMessageFormatted):
            print(event, end="\n------\n")
        elif isinstance(event, (RoomMessageMedia, RoomEncryptedMedia)):
            await save_media(client, event)
        elif isinstance(event, RedactedEvent):
            print(event, end="\n------\n")


async def main() -> None:
    try:
        client = await create_client()
        sync_filter = {"room": {"timeline": {"limit": 1}}}
        sync_resp = await client.sync(full_state=True, sync_filter=sync_filter)
        while True:
            room = await select_room(client)
            await room_messages(client, sync_resp, room)
    except Exception as e:
        print(e)
    finally:
        await client.logout()
        await client.close()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
