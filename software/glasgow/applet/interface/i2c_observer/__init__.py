import argparse
import logging
import enum
import asyncio
from sys import is_finalizing
from amaranth import *

from ....cli_colors import cli_green,cli_red,cli_blue,cli_yellow
from ....gateware.i2c import I2CObserver
from ... import *

class Event(enum.IntEnum):
    START   = 0x10
    STOP    = 0x20
    BYTE    = 0x30
    ACK     = 0x40 # LSB contains value

class I2CObserverSubtarget(Elaboratable):
    def __init__(self, ports, in_fifo, target):
        self.target     = target
        self.ports      = ports
        self.in_fifo    = in_fifo

    def elaborate(self, platform):
        m = Module()

        m.submodules.i2c_observer = i2c_observer = I2CObserver(self.ports)

        m.d.sync += self.in_fifo.w_en.eq(0),
        m.d.sync += self.in_fifo.w_data.eq(Const(0x00, shape=8)),

        with m.FSM():
            byte_val = Signal(8)
            with m.State("TRACE"):
                with m.If(i2c_observer.bus.start):
                    m.d.sync += self.in_fifo.w_data.eq(Event.START),
                    m.d.sync += self.in_fifo.w_en.eq(1),

                with m.If(i2c_observer.bus.stop):
                    m.d.sync += self.in_fifo.w_data.eq(Event.STOP),
                    m.d.sync += self.in_fifo.w_en.eq(1),

                with m.If(i2c_observer.ack):
                    m.d.sync += self.in_fifo.w_data.eq(Event.ACK | i2c_observer.ack_o),
                    m.d.sync += self.in_fifo.w_en.eq(1),

                with m.If(i2c_observer.byte):
                    m.d.sync += self.in_fifo.w_data.eq(Event.BYTE),
                    m.d.sync += self.in_fifo.w_en.eq(1),
                    m.d.sync += byte_val.eq(i2c_observer.byte_o)
                    m.next = "XFER-VAL"

            with m.State("XFER-VAL"):
                m.d.sync += self.in_fifo.w_data.eq(byte_val),
                m.d.sync += self.in_fifo.w_en.eq(1),
                m.next = "TRACE"

        return m

class XferTimeoutError(Exception):
    def __init__(self, xfer) -> None:
        self.xfer = xfer
        super().__init__("A timeout occured while waiting for a transfer to complete")

class I2CObserverApplet(GlasgowApplet):
    logger = logging.getLogger(__name__)
    help = "observe I²C transactions"
    description = """
    Trace transactions on the I²C bus

    This applet allows tracing of any I²C event on a given bus. Events and data are transferred to
    the computer for filtering and printing.
    """
    required_revision = "C0"

    @classmethod
    def add_build_arguments(cls, parser, access):
        super().add_build_arguments(parser, access)
        access.add_pin_argument(parser, "scl", default=True)
        access.add_pin_argument(parser, "sda", default=True)

    def build(self, target, args):
        self.mux_interface = iface = target.multiplexer.claim_interface(self, args)
        iface.add_subtarget(I2CObserverSubtarget(
            ports=iface.get_port_group(scl=args.pin_scl, sda=args.pin_sda),
            in_fifo=iface.get_in_fifo(),
            target=target,
        ))

    @classmethod
    def add_run_arguments(cls, parser, access):
        super().add_run_arguments(parser, access)
        parser.add_argument(
            "--pulls", default=False, action="store_true",
            help="enable integrated pull-ups")
        parser.add_argument(
            "--trace-i2c", type=lambda a: int(a, 0), metavar="I2C-ADDR",
            action='append', help="Trace transactions for this I²C address", default=[])

    async def run(self, device, args):
        pulls = set()
        if args.pulls:
            pulls = {args.pin_scl, args.pin_sda}
        iface = await device.demultiplexer.claim_interface(self, self.mux_interface, args,
                                                           pull_high=pulls)
        return iface

    async def interact(self, device, args, iface):
        counter = 0
        while True:
            try:
                xfer = await self.read_xfer(iface)
                if self.is_filtered(xfer, args):
                    continue
                counter = self.print_xfer(xfer, counter)
            except XferTimeoutError as e:
                self.logger.warning(f"I2C transfer time out")
                self.print_xfer(e.xfer, counter)

    def is_filtered(self, xfer, args):
        event, _ = self.decode_event(xfer[0])
        if event != Event.START.value:
            self.logger.warning(f"xfer does not start with a START event: 0x{event:02x}")
            return False

        if len(xfer) < 3:
            self.logger.warning(f"xfer does contain address {len(xfer)}")
            return False

        address = self.to_7bit_address(xfer[2])
        include = args.trace_i2c
        if len(include) > 0:
            return address not in include

        return False

    def print_xfer(self, events, counter):
        for event, data, is_address in self.iter_xfer(events):
            match (event, is_address):
                case (Event.START, False):
                    counter += 1
                    print(f"{counter:04d} {cli_blue('START')}", end="")
                case (Event.BYTE, True):
                    op = "R" if self.is_read(data) else "W"
                    addr_str = cli_yellow(f"{self.to_7bit_address(data):02x}")
                    print(f" {op}", end="")
                    print(f" {addr_str}", end="")
                case (Event.BYTE, False):
                    print(f" {data:02x}", end="")
                case (Event.ACK, False):
                    ack = cli_green("A") if self.is_ack(data) else cli_red("N")
                    print(f" {ack}", end="")
                case (Event.STOP, False):
                    print(cli_blue(" STOP"))
        return counter

    def iter_xfer(self, xfer):
        is_address = False
        xfer_iter = iter(xfer)
        for e in xfer_iter:
            event, event_data = self.decode_event(e)
            match event:
                case Event.START:
                    yield (event, None, False)
                    is_address = True
                case Event.BYTE:
                    yield (event, next(xfer_iter, None), is_address)
                    is_address = False
                case Event.ACK:
                    yield (event, event_data, False)
                case Event.STOP:
                    yield (event, None, False)

    async def read_xfer(self, iface):
        events = []
        timeout = None
        try:
            while True:
                value = await self.read_byte(iface, timeout)
                timeout = 0.3
                event = value & 0xF0
                match event:
                    case Event.START:
                        events.append(value)
                    case Event.BYTE:
                        events.append(value)
                        events.append(await self.read_byte(iface, timeout))
                    case Event.ACK:
                        events.append(value)
                    case Event.STOP:
                        events.append(value)
                        return events
                    case _:
                        self.logger.warning(f"Unknown Event: {value:02x}")
        except asyncio.TimeoutError as e:
            raise XferTimeoutError(events) from e

    async def read_byte(self, iface, timeout=None):
        return (await asyncio.wait_for(iface.read(1), timeout=timeout))[0]

    def decode_event(self, value):
        return (value & 0xF0, value & 0x0F)

    def is_ack(self, value):
        return ((value & 0x1) != 0x1)

    def is_read(self, address):
        return (address & 0x1) == 0x1

    def to_7bit_address(self, address):
        return (address >> 0x1)

