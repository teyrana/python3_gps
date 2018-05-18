# This file is Copyright (c) 2010 by the GPSD project
# BSD terms apply: see the file COPYING in the distribution root for details.
#
# This code run compatibly under Python 2 and 3.x for x >= 2.
# Preserve this property!
from __future__ import absolute_import, print_function, division

import json
import select
import socket
import sys
import time

from .misc import polystr, polybytes
from .watch_options import *

GPSD_HOST = "localhost"
GPSD_PORT = "2947"


class gpscommon(object):
    """Isolate socket handling and buffering from the protocol interpretation."""

    def __init__(self, host="127.0.0.1", port=GPSD_PORT, verbose=0, should_reconnect=False):
        self.sock = None        # in case we blow up in connect
        self.linebuffer = b''
        self.received = -1 # time of last successful non-zero read
        self.reconnect = should_reconnect
        self.timeout = 10.0
        self.verbose = verbose

        if host is not None:
            self.host = host
        if port is not None:
            self.port = port

        self.bresponse = None
        self.response = None

    def connect(self, host, port):
        """Connect to a host on a given port.

        If the hostname ends with a colon (`:') followed by a number, and
        there is no port specified, that suffix will be stripped off and the
        number interpreted as the port number to use.
        """
        if not port and (host.find(':') == host.rfind(':')):
            i = host.rfind(':')
            if i >= 0:
                host, port = host[:i], host[i + 1:]
            try:
                port = int(port)
            except ValueError:
                raise socket.error("nonnumeric port")

        msg = "getaddrinfo returns an empty list"
        self.sock = None
        for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            af, socktype, proto, _canonname, sa = res
            try:
                self.sock = socket.socket(af, socktype, proto)
                # if self.debuglevel > 0: print 'connect:', (host, port)
                self.sock.connect(sa)
                if self.verbose > 0:
                    print("connected to tcp://{}:{}".format(host, port))
                break

            except ConnectionRefusedError as cre:
                if self.verbose > 1:
                    msg = str(cre) + " (to {}:{})".format(host, port)
                    sys.stderr.write("error: {}\n".format(msg.strip()))
                self.close()
                return  # relatively routine error

            except socket.error as e:
                if self.verbose > 1:
                    msg = str(e) + " (to {}:{})".format(host,port)
                    sys.stderr.write("error: {}\n".format(msg.strip()))
                self.close()

                raise  # propagate to caller

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None

    def __del__(self):
        self.close()

    def waiting(self, timeout=0):
        """Return True if data is ready for the client."""
        if self.linebuffer:
            return True
        (winput, _woutput, _wexceptions) = select.select(
            (self.sock,), (), (), timeout)
        return winput != []

    def read(self):
        """Wait for and read data being streamed from the daemon."""

        if self.sock is None:
            self.connect(self.host, self.port)
            if self.sock is None:
                return -1
            self.stream()

        eol = self.linebuffer.find(b'\n')
        if eol == -1:
            # RTCM3 JSON can be over 4.4k long, so go big
            frag = self.sock.recv(8192)
            self.linebuffer += frag

            if self.verbose > 1:
                sys.stderr.write("poll: read complete.\n")

            if 0 == len(self.linebuffer):
                if self.verbose > 1:
                    sys.stderr.write("poll: no available data: returning -1.\n")
                # Read failed
                return -1

            eol = self.linebuffer.find(b'\n')
            if eol == -1:
                if self.verbose > 1:
                    sys.stderr.write("poll: partial message: returning 0.\n")
                # Read succeeded, but only got a fragment
                self.response = ''  # Don't duplicate last response
                return 0
        else:
            if self.verbose > 1:
                sys.stderr.write("poll: fetching from buffer.\n")

        # We got a line
        eol += 1
        # Provide the response in both 'str' and 'bytes' form
        self.bresponse = self.linebuffer[:eol]
        self.response = polystr(self.bresponse)
        self.linebuffer = self.linebuffer[eol:]

        # Can happen if daemon terminates while we're reading.
        if not self.response:
            return -1
        if self.verbose > 1:
            sys.stderr.write("poll: data is %s\n" % repr(self.response))
        self.received = time.time()
        # We got a \n-terminated line
        return len(self.response)

    # Note that the 'data' method is sometimes shadowed by a name
    # collision, rendering it unusable.  The documentation recommends
    # accessing 'response' directly.  Consequently, no accessor method
    # for 'bresponse' is currently provided.

    def data(self):
        """Return the client data buffer."""
        return self.response

    def send(self, commands):
        """Ship commands to the daemon."""
        if not commands.endswith("\n"):
            commands += "\n"

        if self.sock is not None:
            self.sock.send(polybytes(commands))


class json_error(BaseException):
    def __init__(self, data, explanation):
        BaseException.__init__(self)
        self.data = data
        self.explanation = explanation


class gpsjson(object):
    """Basic JSON decoding."""

    def __init__(self):
        # defined primarily to silence code linter warnings
        self.stream_command = None
        self.data = None
        self.verbose = -1

    def __iter__(self):
        return self

    def unpack(self, buf):
        try:
            self.data = dictwrapper(json.loads(buf.strip(), encoding="ascii"))
        except ValueError as e:
            raise json_error(buf, e.args[0])
        # Should be done for any other array-valued subobjects, too.
        # This particular logic can fire on SKY or RTCM2 objects.
        if hasattr(self.data, "satellites"):
            self.data.satellites = [dictwrapper(x)
                                    for x in self.data.satellites]

    def stream(self, flags=0, devpath=None):
        """Control streaming reports from the daemon,"""

        if 0 < flags:
            self.stream_command = self.generate_stream_command(flags, devpath)

        if self.stream_command:
            if self.verbose > 1:
                sys.stderr.write("send: stream as: {}\n".format(self.stream_command))
            self.send(self.stream_command)
        else:
            raise TypeError("Could not request a stream: Invalid streaming command!")

    def generate_stream_command(self, flags=0, devpath=None):
        if flags & WATCH_OLDSTYLE:
            return self.generate_stream_command_old_style(flags)
        else:
            return self.generate_stream_command_new_style(flags, devpath)

    @staticmethod
    def generate_stream_command_old_style(flags=0):
        if flags & WATCH_DISABLE:
            arg = "w-"
            if flags & WATCH_NMEA:
                arg += 'r-'
                return arg
        elif flags & WATCH_ENABLE:
            arg = 'w+'
            if flags & WATCH_NMEA:
                arg += 'r+'
                return arg

    @staticmethod
    def generate_stream_command_new_style(flags=0, devpath=None):

        if (flags & (WATCH_JSON | WATCH_OLDSTYLE | WATCH_NMEA | WATCH_RAW)) == 0:
            flags |= WATCH_JSON

        if flags & WATCH_DISABLE:
            arg = '?WATCH={"enable":false'
            if flags & WATCH_JSON:
                arg += ',"json":false'
            if flags & WATCH_NMEA:
                arg += ',"nmea":false'
            if flags & WATCH_RARE:
                arg += ',"raw":1'
            if flags & WATCH_RAW:
                arg += ',"raw":2'
            if flags & WATCH_SCALED:
                arg += ',"scaled":false'
            if flags & WATCH_TIMING:
                arg += ',"timing":false'
            if flags & WATCH_SPLIT24:
                arg += ',"split24":false'
            if flags & WATCH_PPS:
                arg += ',"pps":false'
            return arg + "}\n"

        elif flags & WATCH_ENABLE:
            arg = '?WATCH={"enable":true'
            if flags & WATCH_JSON:
                arg += ',"json":true'
            if flags & WATCH_NMEA:
                arg += ',"nmea":true'
            if flags & WATCH_RARE:
                arg += ',"raw":1'
            if flags & WATCH_RAW:
                arg += ',"raw":2'
            if flags & WATCH_SCALED:
                arg += ',"scaled":true'
            if flags & WATCH_TIMING:
                arg += ',"timing":true'
            if flags & WATCH_SPLIT24:
                arg += ',"split24":true'
            if flags & WATCH_PPS:
                arg += ',"pps":true'
            if flags & WATCH_DEVICE:
                arg += ',"device":"%s"' % devpath
            return arg + "}\n"

        else:
            return ""



class dictwrapper(object):
    "Wrapper that yields both class and dictionary behavior,"

    def __init__(self, ddict):
        self.__dict__ = ddict

    def get(self, k, d=None):
        return self.__dict__.get(k, d)

    def keys(self):
        return self.__dict__.keys()

    def __getitem__(self, key):
        "Emulate dictionary, for new-style interface."
        return self.__dict__[key]

    def __setitem__(self, key, val):
        "Emulate dictionary, for new-style interface."
        self.__dict__[key] = val

    def __contains__(self, key):
        return key in self.__dict__

    def __str__(self):
        return "<dictwrapper: " + str(self.__dict__) + ">"
    __repr__ = __str__

    def __len__(self):
        return len(self.__dict__)

#
# Someday a cleaner Python interface using this machinery will live here
#

# End
