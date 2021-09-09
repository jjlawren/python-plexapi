# -*- coding: utf-8 -*-
"""Client timeline subscription callback handler.

Example script:
    #!/usr/bin/env python3
    from aiohttp import ClientSession
    import asyncio
    from functools import partial
    from plexapi.server import PlexServer
    from plexapi.timeline import ClientTimelineManager

    baseurl = 'https://<PLEX_SERVER_ADDRESS>:32400'
    token = '<TOKEN>'

    server = PlexServer(baseurl, token=token)

    def print_timeline(client, timeline):
        if timeline:
            print(f"{client}: {timeline.__dict__}")
        else:
            print(f"{client} is stopped")

    async def main(timelines):
        session = ClientSession()
        for client in server.clients():
            callback = partial(print_timeline, client)
            tl = ClientTimelineManager.get_or_create(client, callback=callback, session=session)
            await tl.async_subscribe()

        async def before_shutdown():
            await ClientTimelineManager.async_unsubscribe_all()
            await session.close()

        await asyncio.sleep(100)
        await before_shutdown()

    if __name__ == "__main__":
        asyncio.run(main())
"""

import asyncio
import socket
from xml.etree import ElementTree

from aiohttp import ClientError, web

from plexapi import log, utils
from plexapi.base import PlexObject
from plexapi.exceptions import BadRequest, Unsupported

DEFAULT_LISTEN_PORT = 32500
TIMELINE_TYPES = ["music", "photo", "video"]


class SubscriptionsMap:
    def __init__(self):
        self.subscriptions = {}

    def all(self):
        """Return all known ClientTimelineManager instances."""
        return list(self.subscriptions.values())

    def register(self, subscription):
        """Register a new subscription"""
        self.subscriptions[subscription.client.machineIdentifier] = subscription

    def unregister(self, subscription):
        """Unregister a subscription"""
        self.subscriptions.pop(subscription.client.machineIdentifier, None)

    def get_subscription(self, machineIdentifier):
        """Look up a subscription"""
        return self.subscriptions.get(machineIdentifier)

    @property
    def count(self):
        """
        `int`: The number of active subscriptions.
        """
        return len(self.subscriptions)


class EventHandler:
    def __init__(self, subscriptions_map):
        self.subscriptions_map = subscriptions_map

    async def timeline_callback(self, request):
        data = await request.text()
        content = ElementTree.fromstring(data) if data.strip() else None
        if not content:
            return

        machineIdentifier = request.headers.get("X-Plex-Client-Identifier")
        if not machineIdentifier:
            log.error("No identifier found in timeline callback")
            return

        subscription = self.subscriptions_map.get_subscription(machineIdentifier)
        if not subscription:
            log.error(
                "No matching subscription for %s found in %s",
                machineIdentifier,
                self.subscriptions_map.subscriptions,
            )
            return

        active_timeline = subscription.update(content)
        await subscription.send_event(active_timeline)

        return web.Response(text="OK", status=200)


class EventListener:

    subscriptions_map = SubscriptionsMap()

    def __init__(self, port=None):
        """Initialize the EventListener."""
        super().__init__()
        self.sock = None
        self.address = ()
        self.ip_address = None
        self.is_running = False
        self.port = None
        self.requested_port = port or DEFAULT_LISTEN_PORT
        self.runner = None
        self.site = None
        self.start_stop_lock = None

    async def async_start(self, ip_address):
        """Start the event listener listening on the local machine under the lock.

        Args:
            ip_address (str): The IP address of the local interface to listen on.
        """
        if not self.start_stop_lock:
            self.start_stop_lock = asyncio.Lock()
        async with self.start_stop_lock:
            if self.is_running:
                return
            port = await self.async_listen(ip_address)
            if not port:
                return
            self.address = (ip_address, port)
            self.is_running = True
            log.debug("Event listener running on %s:%d", self.ip_address, self.port)

    async def async_listen(self, ip_address):
        """Start the event listener listening on the local machine at
        port 32500 (default). If this port is unavailable, the
        listener will attempt to listen on the next available port,
        within a range of 100.

        Make sure that your firewall allows inbound connections to this port.

        Handling of requests is delegated to an `EventHandler` instance.

        Args:
            ip_address (str): The IP address of the local interface to listen on.

        Returns:
            int: The port on which the server is listening.
        """
        for port_number in range(self.requested_port, self.requested_port + 100):
            try:
                if port_number > self.requested_port:
                    log.debug("Trying next port (%d)", port_number)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind((ip_address, port_number))
                sock.listen(200)
                self.sock = sock
                self.port = port_number
                break
            except socket.error as e:
                log.warning("Could not bind to %s:%s: %s", ip_address, port_number, e)
                continue

        if not self.port:
            return None

        self.ip_address = ip_address
        await self._async_start()
        return self.port

    async def _async_start(self):
        """Start the subscription listener."""
        handler = EventHandler(self.subscriptions_map)
        app = web.Application()
        app.add_routes([web.route("post", "/:/timeline", handler.timeline_callback)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.SockSite(self.runner, self.sock)
        await self.site.start()

    async def async_stop(self):
        """Stop the listener."""
        async with self.start_stop_lock:
            if self.site:
                await self.site.stop()
                self.site = None
            if self.runner:
                await self.runner.cleanup()
                self.runner = None
            if self.sock:
                self.sock.close()
                self.sock = None
            self.port = None
            self.ip_address = None
            self.is_running = False


class ClientTimelineManager:
    """Client timeline and subscription manager."""

    event_listener = EventListener()
    subscriptions_map = event_listener.subscriptions_map

    def __init__(self, client, callback=None, session=None):
        """Initialize a ClientTimelineManager instance.

        Args:
            client (PlexClient): The Plex client to manage timelines.
            callback (Coroutine): A coroutine which will be called when timeline subscription events arrive.
                This callback needs to accept one positional argument:
                * timeline (ClientTimeline): The active timeline session, or None if playback is stopped.
            session (aiohttp.ClientSession): An optional aiohttp session to use in async requests.
        """
        self.client = client
        self.callback = callback
        self.timelines = {}
        self._session = session

        if client._baseurl and "127.0.0.1" in client._baseurl:
            self.client.proxyThroughServer(True)

        self.subscription_lock = None
        self._auto_renew_task = None
        self.is_subscribed = False
        self._failed_autorenews = 0
        self._retry_limit = 3

    @classmethod
    def get_or_create(cls, client, callback=None, session=None):
        """Return an existing matching subscription for this client or create a new one."""
        timeline_manager = cls.subscriptions_map.get_subscription(
            client.machineIdentifier
        )
        if timeline_manager:
            return timeline_manager
        return cls(client, callback=callback, session=session)

    @classmethod
    async def async_unsubscribe_all(cls):
        """Unsubscribe from all active timeline subscriptions."""
        for subscription in cls.subscriptions_map.all():
            await subscription.async_unsubscribe()

    def update(self, data):
        """Update the cache of timelines using ElementTree data."""
        active_timeline = None
        for entry in data:
            entry_type = entry.attrib.get("type")
            timeline = self.timelines.get(entry_type)
            if timeline:
                timeline._loadData(entry)
            else:
                timeline = ClientTimeline(self.client, entry)
                self.timelines[timeline.type] = timeline
            if timeline.state != "stopped":
                active_timeline = timeline
        return active_timeline

    def poll(self, wait=0):
        """Poll the clients timelines, create, and return timeline objects.
        Some clients may not always respond to timeline requests, believe this
        to be a Plex bug.
        """
        timelines = self.client.sendCommand("/timeline/poll", wait=wait)
        if not timelines:
            self.timelines = {}
            return
        return self.update(timelines)

    async def async_sendCommand(self, command, proxy=None, **params):
        """Convenience wrapper around :func:`~plexapi.client.PlexClient.async_query` to more easily
        send simple commands to a client. Returns an ElementTree object containing
        the response.

        Parameters:
            client (PlexClient): The Plex client to direct the command.
            command (str): Command to be sent in for format '<controller>/<command>'.
            proxy (bool): Set True to proxy this command through the PlexServer.
            **params (dict): Additional GET parameters to include with the command.

        Raises:
            :exc:`~plexapi.exceptions.Unsupported`: When we detect the client doesn't support this capability.
        """
        command = command.strip("/")
        controller = command.split("/")[0]
        headers = {"X-Plex-Target-Client-Identifier": self.client.machineIdentifier}
        if controller not in self.client.protocolCapabilities:
            raise Unsupported(
                f"Client {self.client.title} does not support {controller} controls"
            )

        proxy = self.client._proxyThroughServer if proxy is None else proxy
        async_query = (
            self.client._server.async_query if proxy else self.client.async_query
        )

        params["commandID"] = self.client._nextCommandId()
        key = f"/player/{command}{utils.joinArgs(params)}"

        try:
            return await async_query(key, headers=headers)
        except ElementTree.ParseError:
            # Workaround for players which don't return valid XML on successful commands
            #   - Plexamp, Plex for Android: `b'OK'`
            #   - Plex for Samsung: `b'<?xml version="1.0"?><Response code="200" status="OK">'`
            if self.client.product in (
                "Plexamp",
                "Plex for Android (TV)",
                "Plex for Android (Mobile)",
                "Plex for Samsung",
            ):
                return
            raise

    async def _async_cancel_subscription(self):
        self.subscriptions_map.unregister(self)

        if self.subscriptions_map.count == 0:
            log.debug("Shutting down event listener")
            await self.event_listener.async_stop()
        else:
            log.debug(
                "Not shutting down event listener, %s subscriptions remain",
                self.subscriptions_map.count,
            )

        self.is_subscribed = False
        self._failed_autorenews = 0
        self._auto_renew_cancel()

    def _auto_renew_start(self, interval=30):
        """Starts the auto_renew loop."""
        self._auto_renew_task = asyncio.get_event_loop().call_later(
            interval, self._auto_renew_run, interval
        )

    def _auto_renew_run(self, interval):
        asyncio.ensure_future(self.async_renew())
        self._auto_renew_start(interval)

    def _auto_renew_cancel(self):
        """Cancels the auto_renew loop."""
        if self._auto_renew_task:
            self._auto_renew_task.cancel()
            self._auto_renew_task = None

    async def _async_subscribe(self):
        """Send the subscription request to the client.

        Used in both new subscriptions and subscription renewals.
        """
        return await self.async_sendCommand(
            "/timeline/subscribe", protocol="http", port=self.event_listener.port
        )

    async def async_subscribe(self, auto_renew=True):
        """Subscribe to a client's timeline updates."""
        if not self.subscription_lock:
            self.subscription_lock = asyncio.Lock()

        if self._session:
            if not self.client._async_session:
                self.client._async_session = self._session
            if not self.client._server._async_session:
                self.client._server._async_session = self._session

        async with self.subscription_lock:
            if self.is_subscribed:
                return

            if not self.event_listener.is_running:
                await self.event_listener.async_start("10.0.10.66")

            try:
                self.subscriptions_map.register(self)
                await self._async_subscribe()
            except (
                BadRequest,
                ClientError,
                Unsupported,
                asyncio.exceptions.TimeoutError,
            ) as exc:
                log.debug("Subscription to %s failed: %s", self.client, exc)
                await self.async_unsubscribe()
            else:
                log.debug("Subscription to %s successful: %s", self.client, self)
                self.is_subscribed = True
                self._auto_renew_start()

            return self

    async def async_renew(self):
        """Handle renewals of existing subscriptions."""
        try:
            await self._async_subscribe()
        except (BadRequest, ClientError, asyncio.exceptions.TimeoutError) as exc:
            self._failed_autorenews += 1
            if self._failed_autorenews >= self._retry_limit:
                log.debug(
                    "Autorenew to %s failed after %s attempts, unsubscribing [%s: %s]",
                    self.client,
                    self._failed_autorenews,
                    type(exc),
                    exc,
                )
                await self._async_cancel_subscription()
            else:
                log.debug(
                    "Autorenew to %s failed, will attempt again [%s: %s]",
                    self.client,
                    type(exc),
                    exc,
                )
        except Exception:
            log.exception(
                "Autorenew to %s failed with unknown reason, unsubscribing", self.client
            )
        else:
            self._failed_autorenews = 0

    async def async_unsubscribe(self):
        """Unsubscribe from an existing subscription."""
        if not self.is_subscribed:
            await self._async_cancel_subscription()
            return

        log.debug("Unsubscribing from %s", self.client)

        await self._async_cancel_subscription()
        try:
            return await self.async_sendCommand("/timeline/unsubscribe")
        except (ClientError, asyncio.exceptions.TimeoutError) as exc:
            log.debug("Could not unsubscribe from %s: %s", self.client, exc)

    async def send_event(self, timeline):
        """Send a received timeline event to the callback coroutine."""
        if self.callback and hasattr(self.callback, "__call__"):
            await self.callback(timeline)
        else:
            log.warning("Error sending %s to %s", timeline, self.callback)


class ClientTimeline(PlexObject):
    """Represents a single client timeline response."""

    def __init__(self, client, data=None, initpath=None):
        super().__init__(client._server, data, initpath)
        self.client = client

    def _loadData(self, data):
        self._data = data
        self.address = data.attrib.get("address")
        self.adDuration = data.attrib.get("adDuration")
        self.adState = data.attrib.get("adState")
        self.adTime = data.attrib.get("adTime")
        self.audioStreamId = utils.cast(int, data.attrib.get("audioStreamId"))
        self.autoPlay = utils.cast(bool, data.attrib.get("autoPlay"))
        self.bandwidth = data.attrib.get("bandwidth")
        self.bufferedSize = data.attrib.get("bufferedSize")
        self.bufferedTime = data.attrib.get("bufferedTime")
        self.context = data.attrib.get("context")
        self.containerKey = data.attrib.get("containerKey")
        self.controllable = data.attrib.get("controllable")
        self.duration = utils.cast(int, data.attrib.get("duration"))
        self.guid = data.attrib.get("guid")
        self.itemType = data.attrib.get("itemType")
        self.key = data.attrib.get("key")
        self.location = data.attrib.get("location")
        self.machineIdentifier = data.attrib.get("machineIdentifier")
        self.mediaIndex = utils.cast(int, data.attrib.get("mediaIndex"))
        self.offline = data.attrib.get("offline")
        self.partCount = utils.cast(int, data.attrib.get("partCount"))
        self.partIndex = utils.cast(int, data.attrib.get("partIndex"))
        self.playbackTime = utils.cast(int, data.attrib.get("playbackTime"))
        self.playQueueID = utils.cast(int, data.attrib.get("playQueueID"))
        self.playQueueItemID = utils.cast(int, data.attrib.get("playQueueItemID"))
        self.playQueueVersion = utils.cast(int, data.attrib.get("playQueueVersion"))
        self.port = utils.cast(int, data.attrib.get("port"))
        self.protocol = data.attrib.get("protocol")
        self.providerIdentifier = data.attrib.get("providerIdentifier")
        self.ratingKey = utils.cast(int, data.attrib.get("ratingKey"))
        self.repeat = utils.cast(bool, data.attrib.get("repeat"))
        self.seekRange = data.attrib.get("seekRange")
        self.shuffle = utils.cast(bool, data.attrib.get("shuffle"))
        self.state = data.attrib.get("state")
        self.subtitleColor = data.attrib.get("subtitleColor")
        self.subtitlePosition = data.attrib.get("subtitlePosition")
        self.subtitleSize = utils.cast(int, data.attrib.get("subtitleSize"))
        self.subtitleStreamID = utils.cast(int, data.attrib.get("subtitleStreamID"))
        self.time = utils.cast(int, data.attrib.get("time"))
        self.timeStalled = data.attrib.get("timeStalled")
        self.timeToFirstFrame = data.attrib.get("timeToFirstFrame")
        self.token = data.attrib.get("token")
        self.type = data.attrib.get("type")
        self.updated = data.attrib.get("updated")
        self.url = data.attrib.get("url")
        self.volume = utils.cast(int, data.attrib.get("volume"))

    def __repr__(self):
        return "<%s>" % ":".join(
            [
                str(p)
                for p in [
                    self.__class__.__name__,
                    self.type,
                    self.client,
                    self.state,
                    self.ratingKey,
                ]
                if p
            ]
        )
