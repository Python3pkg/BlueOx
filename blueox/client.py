# -*- coding: utf-8 -*-
"""
blueox.client
~~~~~~~~

This module provides utilities for writing client applications which connect or use blueox data.

:copyright: (c) 2012 by Rhett Garber
:license: ISC, see LICENSE for more details.

"""
import collections
import logging
import io
import sys
import itertools

import msgpack
import zmq

from . import ports
from . import store

log = logging.getLogger(__name__)


def default_host(host=None):
    """Build a default host string for clients

    This is specifically for the control port, so its NOT for use by loggers.
    We also respect environment variables BLUEOX_CLIENT_HOST and _PORT if
    command line options aren't your thing.
    """
    return ports.default_control_host(host)


def decode_stream(stream):
    """A generator which reads data out of the buffered file stream, unpacks and decodes the blueox events

    This is useful for parsing on disk log files generated by blueoxd
    """

    unpacker = msgpack.Unpacker()

    while True:
        try:
            data = next(stream)
        except StopIteration:
            break

        unpacker.feed(data)

        for msg in unpacker:
            yield msg


def retrieve_stream_host(context, control_host):
    poller = zmq.Poller()
    sock = context.socket(zmq.REQ)
    sock.connect("tcp://%s" % control_host)
    poller.register(sock, zmq.POLLIN)

    sock.send(msgpack.packb({'cmd': 'SOCK_STREAM'}))

    result = dict(poller.poll(5000))
    if sock in result:
        result = msgpack.unpackb(sock.recv())
        host, _ = control_host.split(':')
        return "%s:%d" % (host, result['port'])
    else:
        log.warning("Failed to connect to server")
        return None


def subscribe_stream(control_host, subscribe):
    context = zmq.Context()

    while True:
        stream_host = retrieve_stream_host(context, control_host)
        if stream_host is None:
            return

        sock = context.socket(zmq.SUB)

        prefix = False
        if subscribe:
            if subscribe.endswith('*'):
                prefix = True
                subscription = subscribe[:-1]
            else:
                subscription = subscribe
        else:
            subscription = ""

        sock.setsockopt(zmq.SUBSCRIBE, subscription)
        log.info("Connecting to %s" % (stream_host,))
        sock.connect("tcp://%s" % (stream_host,))

        # Now that we are connected, loop almost forever emiting events.
        # If we fail to receive any events within the specified timeout, we'll quit
        # and verify that we are connected to a valid stream.
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while True:
            result = dict(poller.poll(5000))
            if sock not in result:
                break

            parts = sock.recv_multipart()
            if len(parts) == 2:
                channel, data = parts
                # If the client only want exact matches, we'll skip this guy.
                if not prefix and subscription and channel != subscription:
                    continue

                yield msgpack.unpackb(data)
            else:
                break


def stream_from_s3_store(bucket, type_name, start_dt, end_dt):
    log_files = store.find_log_files_in_s3(bucket, type_name, start_dt, end_dt)

    streams = []
    for lf in log_files:
        data_stream = lf.open(bucket)
        streams.append(decode_stream(data_stream))

    return itertools.chain(*streams)


def stdin_stream():
    stdin = io.open(sys.stdin.fileno(), buffering=0, mode='rb', closefd=False)
    stream = decode_stream(stdin)
    return stream


class Grouper(object):
    """Utility for grouping events and sub-events together.
    
    Events fed into a Grouper are joined by their common 'id'. Encountering the
    parent event type will trigger emitting a list of all events and sub events
    for that single id. 

    This assumes that the parent event will be the last encountered.

    So for example, you might do something like:

        stream = blueox.client.decode_stream(stdin)
        for event_group in client.Grouper(stream):
            ... do some processing of the event group ...

    """

    def __init__(self, stream, max_size=1000):
        self.max_size = max_size
        self.stream = stream
        self.dict = collections.OrderedDict()

    @property
    def size(self):
        return len(self.dict)

    def __iter__(self):
        for event in self.stream:

            while self.size > self.max_size:
                self.dict.popitem(last=False)

            try:
                self.dict[event['id']].append(event)
            except KeyError:
                self.dict[event['id']] = [event]

            if '.' not in event['type']:
                yield self.dict.pop(event['id'])

        raise StopIteration
