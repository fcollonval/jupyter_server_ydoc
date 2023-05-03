# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio
import json
import uuid
from logging import getLogger
from pathlib import Path
from typing import Any, Type

from jupyter_server_fileid.manager import BaseFileIdManager
from jupyter_server.auth import authorized
from jupyter_server.base.handlers import APIHandler, JupyterHandler
from jupyter_server.serverapp import ServerWebApplication
from jupyter_ydoc import ydocs as YDOCS
from tornado import web
from tornado.httputil import HTTPServerRequest
from tornado.websocket import WebSocketHandler
from ypy_websocket.websocket_server import YRoom
from ypy_websocket.ystore import BaseYStore
from ypy_websocket.yutils import YMessageType

from .loaders import FileLoaderMapping
from .rooms import DocumentRoom, TransientRoom
from .utils import JUPYTER_COLLABORATION_EVENTS_URI, LogLevel, decode_file_path
from .websocketserver import JupyterWebsocketServer

YFILE = YDOCS["file"]

SERVER_SESSION = str(uuid.uuid4())


class YDocWebSocketHandler(WebSocketHandler, JupyterHandler):
    """`YDocWebSocketHandler` uses the singleton pattern for ``WebsocketServer``,
    which is a subclass of ypy-websocket's ``WebsocketServer``.

    In ``WebsocketServer``, we expect to use a WebSocket object as follows:

    - receive messages until the connection is closed with
       ``for message in websocket: pass``.
    - send a message with `await websocket.send(message)`.

    Tornado's WebSocket API is different, so ``YDocWebSocketHandler`` needs to be adapted:

    - ``YDocWebSocketHandler`` is an async iterator, that will yield received messages.
       Messages received in Tornado's `on_message(message)` are put in an async
       ``_message_queue``, from which we get them asynchronously.
    - The ``send(message)`` method is async and calls Tornado's ``write_message(message)``.
    - Although it's currently not used in ypy-websocket, ``recv()`` is an async method for
       receiving a message.
    """
    _message_queue: asyncio.Queue[Any]

    def initialize(
        self,
        websocket_server: JupyterWebsocketServer,
        file_id_manager: BaseFileIdManager,
        file_loaders: FileLoaderMapping,
        ystore_class: Type[BaseYStore],
        document_cleanup_delay: int | None = 60,
        document_save_delay: int | None = 1
    ):
        # CONFIG
        self._file_id_manager = file_id_manager
        self._file_loaders = file_loaders
        self._cleanup_delay = document_cleanup_delay
        self._websocket_server = websocket_server

        self._message_queue = asyncio.Queue()

        # Get room
        self._room_id: str = self.request.path.split("/")[-1]

        if self._websocket_server.room_exists(self._room_id):
            self.room: YRoom = self._websocket_server.get_room(self._room_id)

        else:
            if self._room_id.count(":") >= 2:
                # DocumentRoom
                file_format, file_type, file_id = decode_file_path(self._room_id)
                if self._file_loaders.get_loaders_from_file_id(file_id):
                    self._emit(
                        LogLevel.WARNING,
                        None,
                        "There is another collaborative session accessing the same file.\nThe synchronization between rooms is not supported and you might lose some of your changes.",
                    )

                file = self._file_loaders[self._room_id]
                path = self._file_id_manager.get_path(file_id)
                path = Path(path)
                updates_file_path = str(path.parent / f".{file_type}:{path.name}.y")
                ystore = ystore_class(path=updates_file_path, log=self.log)
                self.room = DocumentRoom(
                    self._room_id,
                    file_format,
                    file_type,
                    file,
                    self.event_logger,
                    ystore,
                    self.log,
                    document_save_delay,
                )

            else:
                # TransientRoom
                # it is a transient document (e.g. awareness)
                self.room = TransientRoom(self._room_id, self.log)

            self._websocket_server.add_room(self._room_id, self.room)

    @property
    def path(self):
        """
        Returns the room id. It needs to be called 'path' for compatibility with
        the WebsocketServer (websocket.path).
        """
        return self._room_id

    @property
    def max_message_size(self):
        """
        Override max_message size to 1GB
        """
        return 1024 * 1024 * 1024

    def __aiter__(self):
        # needed to be compatible with WebsocketServer (async for message in websocket)
        return self

    async def __anext__(self):
        # needed to be compatible with WebsocketServer (async for message in websocket)
        message = await self._message_queue.get()
        if not message:
            raise StopAsyncIteration()
        return message

    async def get(self, *args, **kwargs):
        """
        Overrides default behavior to check whether the client is authenticated or not.
        """
        if self.get_current_user() is None:
            self.log.warning("Couldn't authenticate WebSocket connection")
            raise web.HTTPError(403)
        return await super().get(*args, **kwargs)

    async def open(self, room_id):
        """
        On connection open.
        """
        task = asyncio.create_task(self._websocket_server.serve(self))
        self._websocket_server.background_tasks.add(task)
        task.add_done_callback(self._websocket_server.background_tasks.discard)

        if isinstance(self.room, DocumentRoom):
            # Close the connection if the document session expired
            session_id = self.get_query_argument("sessionId", "")
            if SERVER_SESSION != session_id:
                self.close(1003, f"Document session {session_id} expired")

            # cancel the deletion of the room if it was scheduled
            if self.room.cleaner is not None:
                self.room.cleaner.cancel()

            # Initialize the room
            await self.room.initialize()

            self._emit(LogLevel.INFO, "initialize", "New client connected.")

    async def send(self, message):
        """
        Send a message to the client.
        """
        # needed to be compatible with WebsocketServer (websocket.send)
        try:
            self.write_message(message, binary=True)
        except Exception as e:
            self.log.debug("Failed to write message", exc_info=e)

    async def recv(self):
        """
        Receive a message from the client.
        """
        message = await self._message_queue.get()
        return message

    def on_message(self, message):
        """
        On message receive.
        """
        message_type = message[0]
        if message_type == YMessageType.AWARENESS:
            # awareness
            skip = False
            changes = self.room.awareness.get_changes(message[1:])
            added_users = changes["added"]
            removed_users = changes["removed"]
            for i, user in enumerate(added_users):
                u = changes["states"][i]
                if "user" in u:
                    name = u["user"]["name"]
                    self._websocket_server.connected_users[user] = name
                    self.log.debug("Y user joined: %s", name)
            for user in removed_users:
                if user in self._websocket_server.connected_users:
                    name = self._websocket_server.connected_users[user]
                    del self._websocket_server.connected_users[user]
                    self.log.debug("Y user left: %s", name)
            # filter out message depending on changes
            if skip:
                self.log.debug(
                    "Filtered out Y message of type: %s",
                    YMessageType(message_type).name,
                )
                return skip

        self._message_queue.put_nowait(message)
        self._websocket_server.ypatch_nb += 1

    def on_close(self) -> None:
        """
        On connection close.
        """
        # stop serving this client
        self._message_queue.put_nowait(b"")
        if isinstance(self.room, DocumentRoom) and self.room.clients == [self]:
            # no client in this room after we disconnect
            # keep the document for a while in case someone reconnects
            self.log.info("Cleaning room: %s", self._room_id)
            self.room.cleaner = asyncio.create_task(self._clean_room())

    def _emit(self, level: LogLevel, action: str | None = None, msg: str | None = None) -> None:
        _, _, file_id = decode_file_path(self._room_id)
        path = self._file_id_manager.get_path(file_id)

        data = {"level": level.value, "room": self._room_id, "path": path}
        if action:
            data["action"] = action
        if msg:
            data["msg"] = msg

        self.event_logger.emit(schema_id=JUPYTER_COLLABORATION_EVENTS_URI, data=data)

    async def _clean_room(self) -> None:
        """
        Async task for cleaning up the resources.

        When all the clients of a room leave, we setup a task to clean up the resources
        after a certain amount of time. We need to wait a few seconds to clean up the room
        because sometimes websockets unintentionally disconnect.

        During the clean up, we need to delete the room to free resources since the room
        contains a copy of the document. In addition, we remove the file if there is no rooms
        subscribed to it.
        """
        assert isinstance(self.room, DocumentRoom)

        if self._cleanup_delay is None:
            return

        await asyncio.sleep(self._cleanup_delay)

        # Remove the room from the websocket server
        self.log.info("Deleting Y document from memory: %s", self.room.room_id)
        self._websocket_server.delete_room(room=self.room)

        # Clean room
        del self.room
        self.log.info("Room %s deleted", self._room_id)
        self._emit(LogLevel.INFO, "clean", "Room deleted.")

        # Clean the file loader if there are not rooms using it
        file = self._file_loaders[self._room_id]
        if file.number_of_subscriptions == 0:
            self.log.info("Deleting file %s", file.path)
            del self.files[self._room_id]
            self._emit(LogLevel.INFO, "clean", "Loader deleted.")

    def check_origin(self, origin):
        """
        Check origin
        """
        return True


class DocSessionHandler(APIHandler):
    """
    Jupyter Server's handler to retrieve the document's session.
    """

    auth_resource = "contents"

    @web.authenticated
    @authorized
    async def put(self, path):
        """
        Creates a new session for a given document or returns an existing one.
        """
        body = json.loads(self.request.body)
        format = body["format"]
        content_type = body["type"]
        file_id_manager = self.settings["file_id_manager"]

        idx = file_id_manager.get_id(path)
        if idx is not None:
            # index already exists
            self.log.info("Request for Y document '%s' with room ID: %s", path, idx)
            data = json.dumps(
                {"format": format, "type": content_type, "fileId": idx, "sessionId": SERVER_SESSION}
            )
            self.set_status(200)
            return self.finish(data)

        # try indexing
        idx = file_id_manager.index(path)
        if idx is None:
            # file does not exists
            raise web.HTTPError(404, f"File {path!r} does not exist")

        # index successfully created
        self.log.info("Request for Y document '%s' with room ID: %s", path, idx)
        data = json.dumps(
            {"format": format, "type": content_type, "fileId": idx, "sessionId": SERVER_SESSION}
        )
        self.set_status(201)
        return self.finish(data)
